"""서버 STT 어댑터 — Google Cloud Speech-to-Text 스트리밍. **v1·v2 두 경로가 나란히 산다.**

core 어댑터 규율(도메인/DB 무지, graceful degradation): 키 부재·미설치·인증실패·STT_FAKE
어느 경우든 죽지 않고 **페이크 스트림**으로 폴백한다(과금 0, 서버 정상 기동). tts.py 와 동일한
lru_cache + None 폴백 패턴.

── v1 (발음 챌린지 — 단어 낭독 채점) ────────────────────────────────────────────
세션(stt_session)은 프로바이더를 모른 채 이 인터페이스만 쓴다:
  start()            스트림 개시
  push_audio(bytes)  마이크 PCM(LINEAR16) 청크 투입
  feed_test(text)    테스트 훅 — 실제 스트림은 no-op, 페이크는 final 결과로 방출
  results()          (text, is_final) 를 yield 하는 async iterator
  close()            정리
⛔ v1 경로(get_speech_client·GoogleSttStream·FakeSttStream·make_stt_stream)는 **일부러 v1**
   이다. 단어 단발(~60s) 인식에 맞춰 튜닝돼 있고 이미 프로덕션에서 돈다 — 건드리지 않는다.

── v2 (캐스케이드 통화 — 턴 감지) ──────────────────────────────────────────────
v1 에는 **음성 활동 이벤트가 없다.** is_final 만으로는 턴 "시작"을 알 수 없어 barge-in 이
불가능하다. v2 의 enable_voice_activity_events 가 SPEECH_ACTIVITY_BEGIN/END 를 준다 —
시작은 barge-in 트리거, 종료는 턴 종료 판정이다. 그래서 v1 을 고치지 않고 **나란히** 둔다.
  start() / push_audio() / feed_test() / feed_test_event() / events() / close()
  events() 는 (text, is_final) 튜플이 아니라 SttV2Event 를 yield 한다(VAD 이벤트를 담아야 해서).
설계: docs/20260805_1720_캐스케이드-턴감지-최소루프-설계.md
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.config import settings

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_SENTINEL = object()  # 큐 종료 센티널


@lru_cache(maxsize=1)
def get_speech_client() -> "Any | None":
    """SpeechAsyncClient(프로세스당 1개). STT_FAKE·키부재·미설치·인증실패면 None(graceful).

    STT 전용 키(STT_SA_KEY_FILE)가 있으면 그걸, 없으면 통화/TTS 용 키(TTS_SA_KEY_FILE,
    bt-dev-web-01)를 재사용한다 — 같은 GCP 프로젝트라 Speech-to-Text API 활성화 + SA 에
    roles/speech.client 만 있으면 된다.
    """
    if settings.STT_FAKE:
        logger.warning("[stt] STT_FAKE 활성 — 실제 Speech 클라이언트 미생성(페이크 스트림 사용).")
        return None
    try:
        key_path = Path(settings.STT_SA_KEY_FILE or settings.TTS_SA_KEY_FILE)
        if not key_path.is_file():
            logger.warning("[stt] SA 키 없음(%s) → STT 비활성(페이크 폴백).", key_path)
            return None
        # 사용 시점에만 import(미설치 환경에서 모듈 로드만으로 죽지 않게 — 페이크 경로는 미호출).
        from google.cloud import speech_v1
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            str(key_path), scopes=_SCOPES
        )
        client = speech_v1.SpeechAsyncClient(credentials=creds)
        logger.info(
            "[stt] Speech-to-Text async client ready (project=%s)",
            getattr(creds, "project_id", None),
        )
        return client
    except Exception as exc:  # noqa: BLE001 - 미설치/인증/임의 예외 graceful
        get_speech_client.cache_clear()
        logger.warning("[stt] Speech 클라이언트 초기화 실패(무시, 페이크 폴백) — %s", exc)
        return None


class GoogleSttStream:
    """google-cloud-speech v1 비동기 스트리밍(streaming_recognize) 래퍼.

    첫 요청은 streaming_config, 이후 요청은 audio_content 청크. WS 바이너리 프레임을
    asyncio.Queue 로 받아 request 제너레이터로 흘리고, 응답을 순회하며 (transcript, is_final)
    를 방출한다.

    ※ Google 스트리밍은 스트림당 ~5분 한도가 있으나 발음 챌린지는 단발(~60s)이라 롤오버는
      두지 않는다(장문 필요 시 여기서 스트림 재시작).
    """

    def __init__(self, client: Any, sample_rate: int, words: list[str]) -> None:
        self._client = client
        self._sample_rate = sample_rate
        self._words = words
        self._audio_q: asyncio.Queue[Any] = asyncio.Queue()
        self._responses: AsyncIterator[Any] | None = None

    def _streaming_config(self) -> Any:
        from google.cloud import speech_v1 as speech

        cfg_kwargs: dict[str, Any] = dict(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=self._sample_rate,
            language_code=settings.STT_LANGUAGE,
            enable_automatic_punctuation=False,
        )
        if settings.STT_MODEL:
            cfg_kwargs["model"] = settings.STT_MODEL
        if self._words:
            cfg_kwargs["speech_contexts"] = [
                speech.SpeechContext(phrases=self._words, boost=settings.STT_PHRASE_BOOST)
            ]
        config = speech.RecognitionConfig(**cfg_kwargs)
        return speech.StreamingRecognitionConfig(
            config=config, interim_results=True, single_utterance=False
        )

    async def _requests(self) -> AsyncIterator[Any]:
        from google.cloud import speech_v1 as speech

        yield speech.StreamingRecognizeRequest(streaming_config=self._streaming_config())
        while True:
            chunk = await self._audio_q.get()
            if chunk is _SENTINEL:
                return
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    async def start(self) -> None:
        # 비동기 클라: streaming_recognize 는 응답 async iterator 로 resolve 되는 awaitable.
        self._responses = await self._client.streaming_recognize(requests=self._requests())

    async def push_audio(self, pcm: bytes) -> None:
        await self._audio_q.put(pcm)

    def feed_test(self, text: str) -> None:  # noqa: D401 - 실제 스트림은 테스트 훅 무시
        return None

    async def results(self) -> AsyncIterator[tuple[str, bool]]:
        if self._responses is None:
            return
        async for response in self._responses:
            for result in response.results:
                if not result.alternatives:
                    continue
                text = result.alternatives[0].transcript or ""
                yield text, bool(result.is_final)

    async def close(self) -> None:
        await self._audio_q.put(_SENTINEL)


class FakeSttStream:
    """크레덴셜 없이 세션/프론트 통합을 구동하는 페이크(STT_FAKE·키부재 시).

    push_audio 는 무시하고, feed_test(text) 로 넣은 텍스트를 {final} 결과로 방출한다.
    라우터가 control {"type":"__test_say","text":...} 를 받으면 세션이 feed_test 를 부른다.
    """

    def __init__(self) -> None:
        self._q: asyncio.Queue[Any] = asyncio.Queue()

    async def start(self) -> None:
        return None

    async def push_audio(self, pcm: bytes) -> None:
        return None

    def feed_test(self, text: str) -> None:
        if text:
            self._q.put_nowait((text, True))

    async def results(self) -> AsyncIterator[tuple[str, bool]]:
        while True:
            item = await self._q.get()
            if item is _SENTINEL:
                return
            yield item  # (text, is_final)

    async def close(self) -> None:
        self._q.put_nowait(_SENTINEL)


def make_stt_stream(sample_rate: int, words: list[str]) -> Any:
    """설정/크레덴셜에 따라 실제(Google) 또는 페이크 STT 스트림을 만든다.

    STT_FAKE 거나 클라이언트를 못 만들면(키부재/미설치/인증실패) 페이크로 폴백한다 — 어떤
    경우든 세션은 정상 동작(graceful).
    """
    client = get_speech_client()
    if client is None:
        return FakeSttStream()
    return GoogleSttStream(client, sample_rate, words)


# ════════════════════════ v2 — 캐스케이드 통화 턴 감지 ════════════════════════
# 위(v1)는 발음 챌린지 전용이고 아래(v2)는 통화 전용이다. 같은 파일에 두되 **심볼이 겹치지
# 않는다** — v1 을 수정하지 않고 나란히 추가하는 것이 이 파일의 규율이다.

SPEECH_BEGIN = "speech_begin"      # 사용자가 말을 시작했다(= barge-in 트리거)
SPEECH_END = "speech_end"          # 엔진이 본 발화 종료(턴 종료 판정은 세션의 서버 타이머)
TRANSCRIPT = "transcript"          # 부분/최종 전사
STREAM_ROLLOVER = "stream_rollover"  # 내부 스트림 교체(진단용 — 턴 판정과 무관)
STREAM_ERROR = "stream_error"      # 복구 불가(세션이 error 로 종료)


@dataclass(slots=True)
class SttV2Event:
    """v2 이벤트 1건.

    v1 의 (text, is_final) 튜플로는 음성활동 이벤트를 표현할 수 없어 타입을 따로 둔다.

    Attributes:
        at: time.monotonic() — **도착 시각**. 지연 계산은 반드시 monotonic 으로(벽시계 무관).
        offset_ms: **오디오 시각**. 이 이벤트가 가리키는 지점이 스트림 시작 기준 몇 ms 인가
            (VAD 이벤트 = `speech_event_offset`, 전사 = `result_end_offset`).
            ⭐ 턴 종료 타이머는 **이 값**으로 잰다. 도착 시각으로 재면 리전 왕복(STT v2 는
            서울·도쿄가 없어 global/us 로 나간다)과 인식 지연이 침묵 임계에 그대로 얹혀
            턴이 늦게 끊긴다. `audio_ms_sent - offset_ms` 가 곧 파이프라인 지연이라,
            둘을 분리해 계측할 수도 있다. 알 수 없으면 -1.
    """

    kind: str
    text: str = ""
    is_final: bool = False
    at: float = field(default_factory=time.monotonic)
    offset_ms: int = -1    # 오디오 타임라인 위치(-1 = 미상)
    gap_ms: int = 0        # STREAM_ROLLOVER 일 때 오디오 공백(ms)
    detail: str = ""       # 진단 문자열(롤오버 사유·오류 메시지)


@lru_cache(maxsize=1)
def get_speech_v2_client() -> "tuple[Any, str] | None":
    """(SpeechAsyncClient, project_id) — 페이크 강제·키부재·미설치·인증실패면 None(graceful).

    키는 v1 과 같은 것을 재사용한다(STT_SA_KEY_FILE → 없으면 TTS_SA_KEY_FILE, bt-dev-web-01).
    v2 는 recognizer 리소스 경로에 **프로젝트 id 가 필요**해서 클라이언트와 함께 돌려준다.
    STT_V2_LOCATION 이 global 이 아니면 리전 엔드포인트로 클라이언트를 만들어야 한다
    (안 그러면 NOT_FOUND 가 난다 — v2 의 흔한 함정).
    """
    if settings.STT_V2_FAKE or settings.STT_FAKE:
        logger.warning("[stt-v2] 페이크 모드 — 실제 Speech v2 클라이언트 미생성.")
        return None
    try:
        key_path = Path(settings.STT_SA_KEY_FILE or settings.TTS_SA_KEY_FILE)
        if not key_path.is_file():
            logger.warning("[stt-v2] SA 키 없음(%s) → 페이크 폴백.", key_path)
            return None
        # 사용 시점 import — 미설치 환경에서 모듈 로드만으로 죽지 않게(v1 과 동일 규율).
        from google.cloud import speech_v2
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            str(key_path), scopes=_SCOPES
        )
        project = (settings.STT_V2_PROJECT or getattr(creds, "project_id", "") or "").strip()
        if not project:
            logger.warning("[stt-v2] project id 를 못 정함(키에 없음, STT_V2_PROJECT 미설정) → 페이크 폴백.")
            return None
        location = (settings.STT_V2_LOCATION or "global").strip()
        client_options = None
        if location != "global":
            from google.api_core.client_options import ClientOptions

            client_options = ClientOptions(api_endpoint=f"{location}-speech.googleapis.com")
        client = speech_v2.SpeechAsyncClient(credentials=creds, client_options=client_options)
        logger.info("[stt-v2] Speech v2 async client ready (project=%s, location=%s)", project, location)
        return client, project
    except Exception as exc:  # noqa: BLE001 - 미설치/인증/임의 예외 graceful
        get_speech_v2_client.cache_clear()
        logger.warning("[stt-v2] 클라이언트 초기화 실패(무시, 페이크 폴백) — %s", exc)
        return None


class GoogleSttV2Stream:
    """speech_v2 스트리밍 **1개**. 수명 관리는 RollingSttV2Stream 이 한다.

    첫 요청 = recognizer + streaming_config, 이후 = audio 청크. 응답에서 두 가지를 뽑는다:
      - speech_event_type: SPEECH_ACTIVITY_BEGIN / SPEECH_ACTIVITY_END (VAD)
      - results[].alternatives[0].transcript (+ is_final)

    ⛔ voice_activity_timeout 은 **턴 감지 노브가 아니다.** proto 원문(2026-08-05 직접 확인):
      "the server will automatically close the stream after the specified duration has
       elapsed after the last VOICE_ACTIVITY speech event has been sent."
      → 800ms 로 두면 0.8초 침묵마다 스트림이 죽는다. 기본 미설정으로 두고, 턴 종료는
      세션(cascade_session)이 **서버 자체 타이머**로 판정한다.
    """

    # proto: "Inline audio bytes to be Recognized. Maximum size for this field is 15 KB
    # per request." → 초과분은 잘라 보낸다(클라 프레임이 크면 여기서 방어).
    _MAX_AUDIO_BYTES = 15 * 1024

    def __init__(self, client: Any, project: str, sample_rate: int) -> None:
        self._client = client
        self._project = project
        self._sample_rate = sample_rate
        self._audio_q: asyncio.Queue[Any] = asyncio.Queue()
        self._responses: AsyncIterator[Any] | None = None

    # ── 설정 조립 ──
    def _recognizer(self) -> str:
        location = (settings.STT_V2_LOCATION or "global").strip()
        name = (settings.STT_V2_RECOGNIZER or "_").strip()
        return f"projects/{self._project}/locations/{location}/recognizers/{name}"

    def _streaming_config(self) -> Any:
        from google.cloud.speech_v2.types import cloud_speech

        config = cloud_speech.RecognitionConfig(
            explicit_decoding_config=cloud_speech.ExplicitDecodingConfig(
                encoding=cloud_speech.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=self._sample_rate,
                audio_channel_count=1,
            ),
            language_codes=[settings.STT_V2_LANGUAGE or settings.STT_LANGUAGE],
            model=settings.STT_V2_MODEL or "long",
        )
        # 스트림 보호 상한(기본 미설정). 문서상 유효 범위 500ms~60s 밖 값은 무시한다 —
        # 잘못 주면 스트림이 통째로 닫히는 필드라 조용히 크램프하지 않고 버린다.
        timeout_kwargs: dict[str, Any] = {}
        for key, value in (
            ("speech_start_timeout", settings.STT_V2_VAD_START_GUARD_MS),
            ("speech_end_timeout", settings.STT_V2_VAD_END_GUARD_MS),
        ):
            if value <= 0:
                continue
            if not (500 <= value <= 60_000):
                logger.warning("[stt-v2] %s=%dms 는 유효 범위(500~60000) 밖 — 무시.", key, value)
                continue
            timeout_kwargs[key] = timedelta(milliseconds=value)
        feature_kwargs: dict[str, Any] = {
            "interim_results": True,
            "enable_voice_activity_events": True,
        }
        if timeout_kwargs:
            feature_kwargs["voice_activity_timeout"] = (
                cloud_speech.StreamingRecognitionFeatures.VoiceActivityTimeout(**timeout_kwargs)
            )
        return cloud_speech.StreamingRecognitionConfig(
            config=config,
            streaming_features=cloud_speech.StreamingRecognitionFeatures(**feature_kwargs),
        )

    async def _requests(self) -> AsyncIterator[Any]:
        from google.cloud.speech_v2.types import cloud_speech

        yield cloud_speech.StreamingRecognizeRequest(
            recognizer=self._recognizer(), streaming_config=self._streaming_config()
        )
        while True:
            chunk = await self._audio_q.get()
            if chunk is _SENTINEL:
                return
            yield cloud_speech.StreamingRecognizeRequest(audio=chunk)

    # ── 인터페이스 ──
    async def start(self) -> None:
        self._responses = await self._client.streaming_recognize(requests=self._requests())

    async def push_audio(self, pcm: bytes) -> None:
        # 15KB 상한을 넘는 프레임은 잘라서 넣는다(클라가 큰 프레임을 보내도 안 깨지게).
        for i in range(0, len(pcm), self._MAX_AUDIO_BYTES):
            await self._audio_q.put(pcm[i : i + self._MAX_AUDIO_BYTES])

    def feed_test(self, text: str) -> None:  # noqa: D401 - 실제 스트림은 테스트 훅 무시
        return None

    def feed_test_event(self, kind: str) -> None:  # noqa: D401 - 동상
        return None

    @staticmethod
    def _offset_ms(duration: Any) -> int:
        """proto Duration → ms. 미상이면 -1(세션이 도착 시각으로 폴백)."""
        if duration is None:
            return -1
        total = getattr(duration, "total_seconds", None)
        if callable(total):  # timedelta (proto-plus 가 변환해 준다)
            return int(total() * 1000)
        seconds = getattr(duration, "seconds", None)
        if seconds is None:
            return -1
        return int(seconds) * 1000 + int(getattr(duration, "nanos", 0)) // 1_000_000

    async def events(self) -> AsyncIterator[SttV2Event]:
        if self._responses is None:
            return
        async for response in self._responses:
            # enum 이름으로 비교한다 — 라이브러리 버전에 따라 멤버 구성이 달라도 안 깨지게.
            event_type = getattr(response, "speech_event_type", None)
            name = getattr(event_type, "name", "") or ""
            # speech_event_offset: "Time offset between the beginning of the audio and
            # event emission" — 턴 타이머는 도착 시각이 아니라 이 오디오 시각으로 잰다.
            event_offset = self._offset_ms(getattr(response, "speech_event_offset", None))
            if name == "SPEECH_ACTIVITY_BEGIN":
                yield SttV2Event(kind=SPEECH_BEGIN, offset_ms=event_offset)
            elif name in ("SPEECH_ACTIVITY_END", "END_OF_SINGLE_UTTERANCE"):
                yield SttV2Event(kind=SPEECH_END, offset_ms=event_offset)
            for result in getattr(response, "results", ()):
                alts = getattr(result, "alternatives", None)
                if not alts:
                    continue
                text = alts[0].transcript or ""
                if not text:
                    continue
                yield SttV2Event(
                    kind=TRANSCRIPT,
                    text=text,
                    is_final=bool(result.is_final),
                    # result_end_offset: "Time offset of the end of this result relative to
                    # the beginning of the audio" = 이 전사가 커버하는 오디오의 끝.
                    offset_ms=self._offset_ms(getattr(result, "result_end_offset", None)),
                )

    async def close(self) -> None:
        await self._audio_q.put(_SENTINEL)


class FakeSttV2Stream:
    """크레덴셜 없이 턴 상태기계를 구동하는 페이크(키부재·미설치·STT_V2_FAKE).

    push_audio 는 무시하고, dev 훅으로 넣은 것만 방출한다:
      feed_test_event("speech_begin"|"speech_end") → VAD 이벤트
      feed_test("텍스트")                          → 최종 전사
    데모 페이지가 이 훅으로 "말 시작 → 전사 → 말 끝"을 손으로 재현해 상태기계·프로토콜을
    크레덴셜 0 으로 검증한다.
    """

    def __init__(self) -> None:
        self._q: asyncio.Queue[Any] = asyncio.Queue()

    async def start(self) -> None:
        return None

    async def push_audio(self, pcm: bytes) -> None:
        return None

    def feed_test(self, text: str) -> None:
        if text:
            self._q.put_nowait(SttV2Event(kind=TRANSCRIPT, text=text, is_final=True))

    def feed_test_event(self, kind: str) -> None:
        if kind in (SPEECH_BEGIN, SPEECH_END):
            self._q.put_nowait(SttV2Event(kind=kind))

    async def events(self) -> AsyncIterator[SttV2Event]:
        while True:
            item = await self._q.get()
            if item is _SENTINEL:
                return
            yield item

    async def close(self) -> None:
        self._q.put_nowait(_SENTINEL)


class RollingSttV2Stream:
    """여러 개의 v2 스트림을 이어붙여 **하나의 연속 이벤트 열**로 보이게 한다.

    왜 필요한가(둘 다 실재하는 이유):
      ① speech_end_timeout 이 턴 종료 시 스트림을 닫을 수 있다 → 턴마다 스트림이 죽는다.
      ② 스트림 자체에 수명 한도가 있다(v1 은 ~5분). 통화는 최대 15분이다.

    턴이 끊기지 않게 하는 방법: 교체 중(gap) 들어온 오디오를 링버퍼에 담아뒀다가 새 스트림에
    **그대로 흘려준다**. 오디오 유실 0, 감지가 gap 만큼 늦을 뿐이다. 선개통(두 스트림 겹치기)
    은 gap 이 0 이지만 겹친 구간이 이중 과금이라 기본값으로 쓰지 않는다(원가가 이 프로젝트의
    동기다).
    """

    _MAX_START_FAILS = 3          # 연속 개시 실패 허용치(넘으면 error 로 포기)
    _HOT_LOOP_GUARD_S = 1.0       # 이 시간 안에 무이벤트로 끝난 스트림은 실패로 센다

    def __init__(self, factory: Any, sample_rate: int) -> None:
        self._factory = factory   # () -> GoogleSttV2Stream
        self._sample_rate = sample_rate
        self._cur: Any | None = None
        self._closed = False
        self._rolling = True       # events() 가 첫 스트림을 열기 전까진 버퍼로
        self._speech_active = False
        self._started_at = 0.0
        self._roll_reason = ""     # 우리가 선제로 굴렸으면 사유가 채워진다
        self._audio_ms = 0.0       # 이 세션에 들어온 오디오 총량(전역 타임라인)
        self._base_ms = 0          # 현재 스트림의 t=0 이 전역 타임라인의 몇 ms 인가
        self._pending: list[bytes] = []
        self._pending_bytes = 0
        self._max_pending = max(
            1, settings.CASCADE_ROLLOVER_BUFFER_MS * sample_rate * 2 // 1000
        )
        self._max_life_s = max(30, settings.STT_V2_STREAM_MAX_S)

    # ── 입력 ──
    def _bytes_to_ms(self, n: int) -> float:
        return n / (self._sample_rate * 2) * 1000.0

    async def push_audio(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        self._audio_ms += self._bytes_to_ms(len(pcm))
        if self._cur is None or self._rolling:
            self._buffer(pcm)
            return
        await self._cur.push_audio(pcm)
        # 수명 만료 선제 롤오버 — **말하는 중이면 미룬다**. 턴을 반토막 내지 않기 위해서이기도
        # 하고, 문서상 "If the stream is closed before speech ends, a SPEECH_ACTIVITY_END
        # event won't be sent" 이라 발화 중 교체하면 **턴 종료 신호 자체를 잃기 때문**이다.
        # 스트림을 닫으면 요청 제너레이터가 끝나고 응답 이터레이터도 끝나 events() 가 다음
        # 스트림으로 넘어간다. 유휴 중엔 이벤트가 안 오므로 여기(오디오가 계속 오는 곳)가
        # 만료를 감지할 수 있는 유일한 자리다.
        if not self._speech_active and (time.monotonic() - self._started_at) >= self._max_life_s:
            self._rolling = True
            self._roll_reason = "limit"
            with contextlib.suppress(Exception):
                await self._cur.close()

    def _buffer(self, pcm: bytes) -> None:
        self._pending.append(pcm)
        self._pending_bytes += len(pcm)
        while self._pending_bytes > self._max_pending and len(self._pending) > 1:
            dropped = self._pending.pop(0)
            self._pending_bytes -= len(dropped)
            logger.warning("[stt-v2] 롤오버 버퍼 초과 — 가장 오래된 %d bytes 폐기.", len(dropped))

    async def _flush(self) -> None:
        """대기 버퍼를 새 스트림에 흘리고, **오프셋 기준점**을 잡는다.

        새 스트림의 오디오 타임라인 t=0 은 "지금 흘려 넣는 버퍼의 첫 바이트"다. 그러니
        전역 타임라인에서 그 지점은 (지금까지 들어온 총량 − 버퍼 길이)다. 이 기준점을
        더해줘야 스트림이 바뀌어도 이벤트 오프셋이 연속된다(안 하면 롤오버마다 0으로
        되돌아가 턴 타이머가 오작동한다).
        """
        if not self._cur:
            return
        self._base_ms = int(max(0.0, self._audio_ms - self._bytes_to_ms(self._pending_bytes)))
        for chunk in self._pending:
            with contextlib.suppress(Exception):
                await self._cur.push_audio(chunk)
        self._pending.clear()
        self._pending_bytes = 0

    def feed_test(self, text: str) -> None:
        if self._cur is not None:
            self._cur.feed_test(text)

    def feed_test_event(self, kind: str) -> None:
        if self._cur is not None:
            self._cur.feed_test_event(kind)

    async def start(self) -> None:
        return None  # 실제 개시는 events() 가 한다(스트림마다 다시 열어야 하므로)

    # ── 출력 ──
    async def events(self) -> AsyncIterator[SttV2Event]:
        fails = 0
        gap_from: float | None = None
        reason = "start"
        while not self._closed:
            stream = self._factory()
            try:
                await stream.start()
            except Exception as exc:  # noqa: BLE001
                fails += 1
                logger.warning("[stt-v2] 스트림 개시 실패(%d/%d) — %s", fails, self._MAX_START_FAILS, exc)
                if fails >= self._MAX_START_FAILS:
                    yield SttV2Event(kind=STREAM_ERROR, detail=f"start_failed: {exc}")
                    return
                await asyncio.sleep(0.2 * fails)
                continue
            self._cur = stream
            self._started_at = time.monotonic()
            self._rolling = False
            self._roll_reason = ""
            await self._flush()
            if gap_from is not None:
                yield SttV2Event(
                    kind=STREAM_ROLLOVER,
                    gap_ms=int((time.monotonic() - gap_from) * 1000),
                    detail=reason,
                )
            saw_event = False
            try:
                async for event in stream.events():
                    saw_event = True
                    if event.kind == SPEECH_BEGIN:
                        self._speech_active = True
                    elif event.kind == SPEECH_END:
                        self._speech_active = False
                    if event.offset_ms >= 0:
                        event.offset_ms += self._base_ms  # 스트림 로컬 → 전역 타임라인
                    yield event
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 스트림 오류는 롤오버로 흡수
                logger.warning("[stt-v2] 스트림 오류(롤오버로 흡수) — %s", exc)
                reason = "error"
            else:
                # 우리가 수명 만료로 굴렸으면 'limit', 아니면 서버가 닫은 것(VAD 종료 등).
                reason = self._roll_reason or "vad_close"
            finally:
                self._rolling = True
                self._cur = None
                with contextlib.suppress(Exception):
                    await stream.close()
            if self._closed:
                break
            # 이벤트 한 건 없이 즉시 끝난 스트림이 반복되면(설정 오류 등) 무한 재개시를 막는다.
            if not saw_event and (time.monotonic() - self._started_at) < self._HOT_LOOP_GUARD_S:
                fails += 1
                if fails >= self._MAX_START_FAILS:
                    yield SttV2Event(kind=STREAM_ERROR, detail="stream_closed_immediately")
                    return
                await asyncio.sleep(0.2 * fails)
            else:
                fails = 0
            gap_from = time.monotonic()

    async def close(self) -> None:
        self._closed = True
        self._rolling = True
        cur, self._cur = self._cur, None
        if cur is not None:
            with contextlib.suppress(Exception):
                await cur.close()


def make_stt_v2_stream(sample_rate: int = 16000) -> Any:
    """캐스케이드용 STT v2 스트림(롤오버 포함). 크레덴셜 없으면 페이크로 폴백(R5).

    반환 객체 인터페이스: start / push_audio / feed_test / feed_test_event / events / close.
    """
    resolved = get_speech_v2_client()
    if resolved is None:
        return FakeSttV2Stream()
    client, project = resolved
    return RollingSttV2Stream(
        lambda: GoogleSttV2Stream(client, project, sample_rate), sample_rate
    )


def stt_v2_engine_name() -> str:
    """현재 v2 경로가 실제 엔진인지 페이크인지 — 데모/로그가 사람에게 알려주는 값."""
    return "fake" if get_speech_v2_client() is None else "v2"
