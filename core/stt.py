"""서버 STT 어댑터 — Google Cloud Speech-to-Text 스트리밍. **v1·v2 두 경로가 나란히 산다.**

⭐ 2026-08-10 부터 **벤더가 하나가 아니다.** `make_stt_v2_stream()` 이 엔진을 고르고
  (`CASCADE_STT_ENGINE`, 기본 `openai`), OpenAI 실시간 전사는 `core/openai_stt.py` 가 맡는다.
  개시가 실패하면 `FallbackSttStream` 이 여기 Google 경로로 갈아탄다 — 그래서 이 파일은
  **폴백 대상이자 기본 경로**다. 어느 쪽이 실제로 돌았는지는 스트림의 `vendor` 가 말한다.

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
import re
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


# ── 다중 언어(1차 자료: Speech-to-Text V2 문서) ─────────────────────────────
# https://cloud.google.com/speech-to-text/v2/docs/multiple-languages (2026-08-08 확인)
#   "You can only use the alternative languages feature with the long, short, and
#    telephony models."                                  → 우리 모델 `long` = 지원 ✓
#   "You can list up to three languages for automatic language recognition."   → 상한 3
#   "Specifying multiple languages is only available in the ... global region and the
#    us and eu multi-regions."                           → 우리 위치 `global` = 지원 ✓
#   "Though you can specify up to three languages, constrain the language list to the
#    bare minimum needed as a best practice. The fewer language codes you specify, the
#    higher the likelihood that Cloud Speech-to-Text successfully selects the correct one."
#                                                        → **꼭 필요한 것만 넣는다**
# 필드 의미(REST 레퍼런스): "If additional languages are provided, recognition result will
#   contain recognition in the most likely language detected." → 순서가 우선순위라는 규정은
#   문서에 **없다**. 우리는 학습 언어를 먼저 적지만 그건 규약이 아니라 우리 의도의 표시다.
STT_V2_MAX_LANGUAGES = 3

# ⛔ **검증한 것만 매핑한다.** 짧은 코드(en)는 STT 코드가 아니다 — BCP-47 지역까지 필요하다
#   (`en-US`, `ko-KR`. 1차 자료: v2 supported-languages 표에서 `long` 모델 지원 확인).
#   나머지 언어(vi/th/mn/…)는 아직 표에서 확인하지 않았다. 근거 없는 추측을 넣으면 조용히
#   인식이 죽으므로 **넣지 않는다** — 모르는 짧은 코드는 경고와 함께 버린다(그 경우 동작은
#   지금과 같다: 학습 언어만 듣는다). 실서비스 배선 때 표 전체를 확인해 채운다.
_STT_LANGUAGE_ALIASES: dict[str, str] = {"en": "en-US", "ko": "ko-KR"}
# 모양 검사(BCP-47 근사): 언어[-문자]**-지역**. 통과한 값은 그대로 벤더에 넘긴다.
# ⭐ **지역이 없으면 거절한다.** v2 지원 표의 코드는 지역까지 있다(en-US · ko-KR · cmn-Hans-CN).
#   그리고 우리 설정의 짧은 코드(`CASCADE_TTS_LANGUAGE="en"`)가 바로 그 모양이라, 통과시키면
#   "설정값을 그대로 STT 에 꽂았는데 조용히 안 들리는" 지금 결함이 형태만 바꿔 되살아난다.
_BCP47_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z]{4})?-([A-Za-z]{2}|\d{3})$")


# ⛔⛔ **이 함수는 캐스케이드 전용이 아니다 — 지울 때 같이 지우지 마라**(2026-08-20).
#   쓰는 곳 셋: ①캐스케이드 STT ②발음 챌린지(`stt_session.py` → `/pron/stt/ws`)
#   ③⭐ **라이브 통화**(`call_session._input_language_codes` → Gemini Live 입력 전사 언어 힌트).
#   라이브가 여기 기댄 이유: 입력 전사를 힌트 없이 열어 뒀더니 짧은 한국어가 다른 언어로
#   찍혔는데(실측 call_id=1097: "다"→`套`, "아주"→`और च`), **캐스케이드가 2026-08-08 에
#   똑같은 결함을 이미 겪고** 이 변환을 만들어 뒀다. 표를 하나 더 만들면 같은 질문에 답이 둘이 된다.
#   ⚠ `_STT_LANGUAGE_ALIASES` 에 언어를 채우면 **세 경로가 같이** 좋아진다(라이브 포함).
#   기록: docs/20260813_0040_캐스케이드-데모잔재-정리목록.md §2-b
def normalize_language_codes(codes: Any, fallback: str = "") -> list[str]:
    """언어 코드 목록을 **벤더가 받을 수 있는 모양**으로 다듬는다.

    하는 일: 별칭 확장(en→en-US) · 표준 대소문자 · 중복 제거(순서 유지) · 상한 3 · 모양이
    틀린 값 폐기. 결과가 비면 폴백 한 개를 쓴다 — **언어 코드가 비면 스트림이 400 으로 죽고,
    그건 통화 전체가 죽는다는 뜻이다**(R5).
    """
    out: list[str] = []
    dropped: list[str] = []
    for raw in list(codes or []):
        code = str(raw or "").strip().replace("_", "-")
        if not code:
            continue
        code = _STT_LANGUAGE_ALIASES.get(code.lower(), code)
        if not _BCP47_RE.match(code):
            dropped.append(str(raw))
            continue
        parts = code.split("-")
        code = "-".join(
            [parts[0].lower()]
            + [p.title() if len(p) == 4 else p.upper() for p in parts[1:]]
        )
        if code not in out:
            out.append(code)
    if dropped:
        logger.warning(
            "[stt-v2] 인식 언어 코드 폐기 %s — 지역까지 있는 BCP-47 이어야 한다(예: en-US). "
            "그 언어는 이 통화에서 안 들린다", dropped,
        )
    if len(out) > STT_V2_MAX_LANGUAGES:
        logger.warning("[stt-v2] 언어 %d개 → 문서 상한 %d개로 자른다: %s",
                       len(out), STT_V2_MAX_LANGUAGES, out)
        out = out[:STT_V2_MAX_LANGUAGES]
    if not out:
        out = normalize_language_codes([fallback]) if fallback else []
    return out


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

    def __init__(self, client: Any, project: str, sample_rate: int,
                 language_codes: list[str] | None = None) -> None:
        self._client = client
        self._project = project
        self._sample_rate = sample_rate
        self._language_codes = list(language_codes or [])
        self._audio_q: asyncio.Queue[Any] = asyncio.Queue()
        self._responses: AsyncIterator[Any] | None = None
        # 과금 계측(원가) — 응답에 실려 오는 total_billed_duration 을 누적한다. 자세한 근거와
        # sum/max 를 둘 다 드는 이유는 _absorb_billing 주석과 설계 §1-1.
        self._billed_sum_ms = 0.0
        self._billed_max_ms = 0.0
        self._billed_msgs = 0

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
            language_codes=list(self._language_codes) or [
                settings.STT_V2_LANGUAGE or settings.STT_LANGUAGE
            ],
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
            self._absorb_billing(response)
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

    def _absorb_billing(self, response: Any) -> None:
        """응답에 실린 과금 초를 누적한다(예외 전량 흡수 — 계측이 통화를 죽이면 안 된다, R5).

        proto 원문(2026-08-07 직접 확인, googleapis master):
          RecognitionResponseMetadata.total_billed_duration
            "When available, billed audio seconds for the corresponding request."
          StreamingRecognizeResponse 에 `RecognitionResponseMetadata metadata = 5` 로 달려 있다.

        ⚠ v1 의 같은 필드는 "billed audio seconds for **the stream** / Set only if this is the
          last response in the stream" 이었다. v2 는 문구가 '해당 요청'으로 바뀌어 **응답마다의
          증분인지, 누적값이 반복해 실리는지 원문만으로 못 정한다.** 그래서 여기서 판정하지
          않고 sum·max·건수를 **셋 다** 들고 간다. 첫 실통화 로그 한 줄이 어느 쪽인지 드러낸다
          (max 를 실제 오디오 길이와 대조하면 갈린다 — 설계 §1-1).
        """
        try:
            meta = getattr(response, "metadata", None)
            if meta is None:
                return
            ms = self._offset_ms(getattr(meta, "total_billed_duration", None))
            if ms <= 0:
                return
            self._billed_sum_ms += ms
            self._billed_max_ms = max(self._billed_max_ms, float(ms))
            self._billed_msgs += 1
        except Exception as exc:  # noqa: BLE001 - 계측 실패는 무시하고 인식은 계속(R5)
            logger.debug("[stt-v2] 과금 메타 누적 실패(무시): %s", exc)

    def usage(self) -> dict:
        """이 스트림 1개가 본 과금 계측(ms). 소유자(RollingSttV2Stream)가 걷어 간다."""
        return {
            "billed_sum_ms": self._billed_sum_ms,
            "billed_max_ms": self._billed_max_ms,
            "billed_msgs": self._billed_msgs,
        }

    async def close(self) -> None:
        await self._audio_q.put(_SENTINEL)


class FakeSttV2Stream:
    """크레덴셜 없이 턴 상태기계를 구동하는 페이크(키부재·미설치·STT_V2_FAKE).

    push_audio 는 인식에 쓰지 않고 **길이만 센다**(원가 줄). 이벤트는 dev 훅으로 넣은 것만:
      feed_test_event("speech_begin"|"speech_end") → VAD 이벤트
      feed_test("텍스트")                          → 최종 전사
    데모 페이지가 이 훅으로 "말 시작 → 전사 → 말 끝"을 손으로 재현해 상태기계·프로토콜을
    크레덴셜 0 으로 검증한다.
    """

    def __init__(self, sample_rate: int = 16000) -> None:
        self._q: asyncio.Queue[Any] = asyncio.Queue()
        # 인식은 안 하지만 **들어온 오디오 길이는 센다** — 데모 세션의 원가 줄이 0.0초로
        # 찍히면 "계측이 안 붙었다"와 "과금이 없었다"를 구분할 수 없다. 과금이 0 이라는
        # 사실은 engine=cascade:fake-stt 가 말한다.
        self._sample_rate = max(1, sample_rate)
        self._sent_ms = 0.0

    async def start(self) -> None:
        return None

    async def push_audio(self, pcm: bytes) -> None:
        self._sent_ms += len(pcm) / (self._sample_rate * 2) * 1000.0

    async def commit(self) -> bool:
        """PTT 턴 경계(페이크) — ⭐ 벤더처럼 **받았다고만** 답한다.

        전사는 테스트가 `feed_test` 로 넣는다. 이 메서드가 없으면 세션의
        `getattr(stream, "commit", None)` 이 None 을 집어 **페이크 위에서 PTT 경로를 아예
        못 태운다**(회귀가 검증할 대상이 사라진다).
        """
        return True

    def feed_test(self, text: str) -> None:
        if text:
            self._q.put_nowait(SttV2Event(kind=TRANSCRIPT, text=text, is_final=True))

    def feed_test_event(self, kind: str) -> None:
        if kind in (SPEECH_BEGIN, SPEECH_END):
            self._q.put_nowait(SttV2Event(kind=kind))

    def usage(self) -> dict:
        # 페이크는 과금이 0이다. 그래도 **같은 모양**을 돌려줘야 호출부가 분기하지 않는다.
        return {"streams": 0, "sent_audio_ms": self._sent_ms, "replay_audio_ms": 0.0,
                "billed_sum_ms": 0.0, "billed_max_ms": 0.0, "billed_msgs": 0}

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

    def __init__(self, factory: Any, sample_rate: int,
                 language_codes: list[str] | None = None) -> None:
        self._factory = factory   # (language_codes) -> GoogleSttV2Stream
        self._sample_rate = sample_rate
        # 이 세션이 **실제로 듣고 있는** 언어들. 개시가 실패하면 첫 언어만 남기고 계속한다
        # (아래 events()). 롤오버로 새 스트림을 열 때도 이 값을 그대로 쓴다.
        self._language_codes = list(language_codes or [])
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
        # ── 원가 계측 ──
        # streams: 스트림 수(요청/스트림 단위 올림이 있으면 여기에 비례해 실청구가 얹힌다).
        # replay_ms: 롤오버 때 **다시 흘려 넣은** 오디오. _audio_ms 는 이 구간을 1회만 세므로
        #   그만큼 우리 카운터가 실청구보다 과소일 수 있다 — 크기를 따로 남긴다(설계 §2-2).
        self._streams = 0
        self._replay_ms = 0.0
        # 이미 최종 전사가 난 오디오의 끝(전역 ms). 롤오버 재생에서 이 앞은 잘라낸다 —
        # 다시 인식되면 같은 발화가 턴 2개가 되고 그 구간이 이중 과금된다.
        self._last_final_ms = -1.0
        self._billed_sum_ms = 0.0
        self._billed_max_ms = 0.0
        self._billed_msgs = 0

    @property
    def language_codes(self) -> list[str]:
        """이 세션이 **지금** 듣고 있는 언어들(강등되면 줄어든다)."""
        return list(self._language_codes)

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

        ⭐ **이미 최종 전사가 난 오디오는 다시 흘리지 않는다**(2026-08-07). 예전엔 버퍼를
        통째로 재생했는데, 그 안에 앞 스트림이 이미 확정한 발화의 꼬리가 들어 있으면 새
        스트림이 그걸 **다시 인식해 같은 최종 전사를 한 번 더** 낸다. 결과는 두 가지로
        동시에 나쁘다: ① 같은 발화가 턴 2개(실통화에서 관측) ② 그 구간 STT 이중 과금
        (원가 설계 §2-2 의 위험이 실제로 발생). 기준은 우리가 **이미 받은 최종 전사의 오디오
        끝**이라 추측이 아니다.
        """
        if not self._cur:
            return
        start_ms = max(0.0, self._audio_ms - self._bytes_to_ms(self._pending_bytes))
        trimmed_ms = 0.0
        if self._last_final_ms > start_ms:
            trimmed_ms = self._trim_pending(self._last_final_ms - start_ms)
            start_ms += trimmed_ms
        self._base_ms = int(start_ms)
        self._replay_ms += self._bytes_to_ms(self._pending_bytes)   # 이중 과금 후보 구간
        logger.info(
            "[stt-v2] 스트림 교체: base_ms=%d 재생=%.0fms 잘라냄=%.0fms audio_ms=%.0f",
            self._base_ms, self._bytes_to_ms(self._pending_bytes), trimmed_ms, self._audio_ms,
        )
        for chunk in self._pending:
            with contextlib.suppress(Exception):
                await self._cur.push_audio(chunk)
        self._pending.clear()
        self._pending_bytes = 0

    def _trim_pending(self, drop_ms: float) -> float:
        """대기 버퍼 앞에서 drop_ms 만큼 버린다(이미 인식이 끝난 구간). 실제로 버린 ms 반환."""
        drop_bytes = int(drop_ms * self._sample_rate * 2 / 1000) & ~1   # 샘플 경계 유지
        dropped = 0
        while drop_bytes > 0 and self._pending:
            head = self._pending[0]
            if len(head) <= drop_bytes:
                self._pending.pop(0)
                self._pending_bytes -= len(head)
                drop_bytes -= len(head)
                dropped += len(head)
            else:
                self._pending[0] = head[drop_bytes:]
                self._pending_bytes -= drop_bytes
                dropped += drop_bytes
                drop_bytes = 0
        return self._bytes_to_ms(dropped)

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
            stream = self._factory(self._language_codes)
            try:
                await stream.start()
            except Exception as exc:  # noqa: BLE001
                fails += 1
                logger.warning("[stt-v2] 스트림 개시 실패(%d/%d) — %s", fails, self._MAX_START_FAILS, exc)
                if self._degrade_languages("개시 실패"):
                    fails = 0
                    continue
                if fails >= self._MAX_START_FAILS:
                    yield SttV2Event(kind=STREAM_ERROR, detail=f"start_failed: {exc}")
                    return
                await asyncio.sleep(0.2 * fails)
                continue
            self._cur = stream
            self._streams += 1
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
                        if event.kind == TRANSCRIPT and event.is_final:
                            # 여기까지는 확정됐다 — 롤오버 재생에서 다시 흘리지 않는다.
                            self._last_final_ms = max(self._last_final_ms, float(event.offset_ms))
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
                self._absorb_usage(stream)
                with contextlib.suppress(Exception):
                    await stream.close()
            if self._closed:
                break
            # 이벤트 한 건 없이 즉시 끝난 스트림이 반복되면(설정 오류 등) 무한 재개시를 막는다.
            if not saw_event and (time.monotonic() - self._started_at) < self._HOT_LOOP_GUARD_S:
                fails += 1
                if self._degrade_languages("이벤트 없이 즉시 종료"):
                    fails = 0
                    gap_from = time.monotonic()
                    continue
                if fails >= self._MAX_START_FAILS:
                    yield SttV2Event(kind=STREAM_ERROR, detail="stream_closed_immediately")
                    return
                await asyncio.sleep(0.2 * fails)
            else:
                fails = 0
            gap_from = time.monotonic()

    def _degrade_languages(self, why: str) -> bool:
        """다중 언어 때문에 스트림이 안 열리면 **첫 언어만 남기고 계속한다**(R5).

        ⛔ 통화가 죽는 것보다 한 언어로라도 듣는 게 낫다. 실패 원인이 언어가 아닐 수도 있지만,
          그 경우에도 이 강등은 손해가 없다(원래 듣던 언어는 그대로 남는다).
        재시도 3회를 다 쓰고 나서가 아니라 **첫 실패에서 바로** 내린다 — 설정이 틀렸다면
        재시도해도 절대 안 열리고, 그 사이 통화는 귀가 먹은 채로 흐른다.
        """
        if len(self._language_codes) <= 1:
            return False
        dropped = self._language_codes[1:]
        self._language_codes = self._language_codes[:1]
        logger.warning(
            "[stt-v2] 다중 언어 강등(%s) — %s 를 빼고 %s 로만 듣는다. 사용자가 모국어로 말하면 "
            "그 발화는 이 통화에서 전사되지 않는다", why, dropped, self._language_codes,
        )
        return True

    def _absorb_usage(self, stream: Any) -> None:
        """끝난 스트림의 과금 계측을 세션 누계로 옮긴다(스트림당 1회, 예외 전량 흡수 R5).

        같은 스트림을 events() 의 finally 와 close() 양쪽에서 만날 수 있어 **객체에 표식을**
        남긴다 — id() 로 판별하면 GC 후 id 재사용에 걸린다.
        """
        try:
            if stream is None or getattr(stream, "_usage_absorbed", False):
                return
            stream._usage_absorbed = True
            u = stream.usage() if hasattr(stream, "usage") else None
            if not u:
                return
            self._billed_sum_ms += float(u.get("billed_sum_ms") or 0.0)
            # 스트림별 최댓값을 더한다 — 값이 '스트림 누적'이면 이 합이 세션 전체 과금이 된다.
            self._billed_max_ms += float(u.get("billed_max_ms") or 0.0)
            self._billed_msgs += int(u.get("billed_msgs") or 0)
        except Exception as exc:  # noqa: BLE001 - 계측 실패로 세션이 죽으면 안 된다(R5)
            logger.debug("[stt-v2] 과금 계측 흡수 실패(무시): %s", exc)

    def usage(self) -> dict:
        """세션 전체 STT 사용량(ms). 캐스케이드 세션이 종료 시 한 번 읽는다."""
        return {
            "streams": self._streams,
            "sent_audio_ms": self._audio_ms,
            "replay_audio_ms": self._replay_ms,
            "billed_sum_ms": self._billed_sum_ms,
            "billed_max_ms": self._billed_max_ms,
            "billed_msgs": self._billed_msgs,
        }

    async def close(self) -> None:
        self._closed = True
        self._rolling = True
        cur, self._cur = self._cur, None
        if cur is not None:
            self._absorb_usage(cur)
            with contextlib.suppress(Exception):
                await cur.close()


class FallbackSttStream:
    """1차 엔진으로 열어 보고, 실패하면 **2차로 갈아탄다**(R5 — 통화가 죽으면 안 된다).

    ⛔ 조용한 폴백 금지. WARNING 을 남기고, `vendor` 를 실제로 돈 엔진 것으로 바꾼다 —
      원가 장부와 로그가 **같은 것을 말해야** 다음 판단이 흔들리지 않는다(TTS 폴백과 같은 설계).
    ⚠ 개시(start)에서만 갈아탄다. 통화 도중 죽으면 그건 스트림 오류 경로(STREAM_ERROR)다 —
      중간에 엔진을 바꾸면 같은 발화가 두 번 인식되거나 사라진다.
    """

    def __init__(self, primary: Any, secondary: Any, primary_name: str) -> None:
        self._primary = primary
        self._secondary = secondary
        self._name = primary_name
        self._cur: Any = None
        self.vendor = ""

    async def start(self) -> None:
        try:
            self._cur = self._primary()
            await self._cur.start()
            self.vendor = getattr(self._cur, "vendor", "") or self._name
            return
        except Exception as exc:  # noqa: BLE001 - 어떤 실패든 통화는 살린다
            logger.warning("[stt] %s 개시 실패 → google 로 폴백한다 — %s",
                           self._name, str(exc)[:160])
            with contextlib.suppress(Exception):
                if self._cur is not None:
                    await self._cur.close()
        self._cur = self._secondary()
        await self._cur.start()
        self.vendor = getattr(self._cur, "vendor", "")

    async def push_audio(self, pcm: bytes) -> None:
        if self._cur is not None:
            await self._cur.push_audio(pcm)

    async def events(self):
        if self._cur is None:
            return
        async for event in self._cur.events():
            yield event

    async def close(self) -> None:
        if self._cur is not None:
            await self._cur.close()

    async def commit(self) -> bool:
        """PTT 턴 경계를 1차 엔진에 내린다.

        ⛔⛔ 이 전달을 빼면 **PTT 가 조용히 죽는다**: 이 래퍼는 메서드를 하나씩 명시 전달하는
          구조라, 없으면 세션의 `getattr(stream, "commit", None)` 이 None 을 집어 commit 이
          영영 안 나가고 — `turn_detection: null` 에서는 **전사도 영영 안 온다.**
        ⚠ 폴백(구글)에는 수동 커밋 개념이 없다 ⇒ False. 세션은 그 값을 보고 전사를 기다리지
          않는다(구글은 전사를 알아서 흘리므로 버튼이 턴 경계라는 성질은 그대로다).
        """
        fn = getattr(self._cur, "commit", None)
        return bool(await fn()) if fn is not None else False

    def feed_test(self, text: str) -> None:
        if hasattr(self._cur, "feed_test"):
            self._cur.feed_test(text)

    def feed_test_event(self, kind: str) -> None:
        if hasattr(self._cur, "feed_test_event"):
            self._cur.feed_test_event(kind)

    def usage(self) -> dict:
        return self._cur.usage() if hasattr(self._cur, "usage") else {}


def make_stt_v2_stream(sample_rate: int = 16000, language_codes: Any = None,
                       *, manual_commit: bool = False) -> Any:
    """캐스케이드용 STT v2 스트림(롤오버 포함). 크레덴셜 없으면 페이크로 폴백(R5).

    반환 객체 인터페이스: start / push_audio / feed_test / feed_test_event / events / close.

    ⭐ `manual_commit` = **PTT 세션**(2026-08-18). 벤더 VAD 를 끄고 턴 경계를 `commit` 으로
      우리가 정한다. ⛔ **OpenAI 에서만 의미가 있다** — 구글엔 수동 커밋 개념이 없어 인자가
      조용히 무시되고 예전처럼 돈다. 그래도 PTT 는 죽지 않는다: 구글은 전사를 계속 흘리므로
      버튼이 턴 경계를 정하는 성질은 그대로다(R5 — 키가 빠졌다고 기능이 죽으면 안 된다).

    ⭐ `language_codes` 는 **여러 개**를 받는다(2026-08-08). 지금까지 한 개(ko-KR)만 넣어서
      **사용자가 모국어로 말하면 전사가 통째로 사라졌다** — 실통화에서 영어 발화 5회 연속
      36초가 `text=''` 로 닫혔다. 우리 사용자는 외국인 학습자이고, 그들은 모국어로 묻고
      한국어로 따라 말한다. 둘 다 들어야 한다.
    """
    codes = normalize_language_codes(
        language_codes, fallback=settings.STT_V2_LANGUAGE or settings.STT_LANGUAGE
    )

    def _google() -> Any:
        resolved = get_speech_v2_client()
        if resolved is None:
            return FakeSttV2Stream(sample_rate)
        client, project = resolved
        return RollingSttV2Stream(
            lambda langs: GoogleSttV2Stream(client, project, sample_rate, langs),
            sample_rate, language_codes=codes,
        )

    if (settings.CASCADE_STT_ENGINE or "google").strip().lower() != "openai":
        return _google()

    from core import openai_stt

    if not openai_stt.is_configured():
        # ⛔ 조용히 넘어가지 않는다 — 어느 엔진이 돌았는지 모르면 실측이 거짓말이 된다.
        logger.warning("[stt] openai 선택됐지만 키가 없다(GPT_API_KEY) → google 로 진행")
        return _google()
    return FallbackSttStream(
        lambda: openai_stt.OpenAiRealtimeSttStream(
            sample_rate, codes, manual_commit=manual_commit),
        _google, "openai"
    )


def stt_v2_engine_name() -> str:
    """현재 v2 경로가 실제 엔진인지 페이크인지 — 데모/로그가 사람에게 알려주는 값."""
    return "fake" if get_speech_v2_client() is None else "v2"
