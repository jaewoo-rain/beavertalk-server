"""서버 STT 어댑터 — Google Cloud Speech-to-Text 스트리밍(발음 챌린지, `/pron/stt/ws`).

⭐⭐ C14-b(2026-09-23) — 옛 "v2"(캐스케이드 통화 턴 감지: SttV2Event·RollingSttV2Stream·
  get_speech_v2_client·FallbackSttStream·make_stt_v2_stream·stt_v2_engine_name, 그리고
  `core/openai_stt.py` 전체)는 캐스케이드 엔진 삭제로 유일한 호출부(cascade_session.py)
  가 없어져 **삭제했다**(사장님 승인). `normalize_language_codes` 만 예외 — v2 섹션
  **안에 있었지만 Live 통화가 쓴다**(`call_session._input_language_codes`, 일본어 STT
  언어힌트 경로)라서 이 파일의 v1 영역(아래)으로 옮겨 살렸다. 본문은 한 글자도
  안 바꿨다(언어 매핑표는 실측으로 굳힌 것).

core 어댑터 규율(도메인/DB 무지, graceful degradation): 키 부재·미설치·인증실패·STT_FAKE
어느 경우든 죽지 않고 **페이크 스트림**으로 폴백한다(과금 0, 서버 정상 기동). tts.py 와 동일한
lru_cache + None 폴백 패턴.

세션(stt_session)은 프로바이더를 모른 채 이 인터페이스만 쓴다:
  start()            스트림 개시
  push_audio(bytes)  마이크 PCM(LINEAR16) 청크 투입
  feed_test(text)    테스트 훅 — 실제 스트림은 no-op, 페이크는 final 결과로 방출
  results()          (text, is_final) 를 yield 하는 async iterator
  close()            정리
⛔ 이 경로(get_speech_client·GoogleSttStream·FakeSttStream·make_stt_stream)는 단어 단발
  (~60s) 인식에 맞춰 튜닝돼 있고 이미 프로덕션에서 돈다 — 건드리지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
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
# ⭐ 2026-09-14(실통화 1607, ja 학습·ko 모국어): ja 가 없어서 `_input_language_codes("ja","ko")` 가 부분 힌트 → «생략» → 입력 전사 무힌트 → 일본어가
#   「保険ってですか」「도움어」 로 찍혀 서버 판정 전부 미통과·재출제. 레지스트리(core.languages) 언어 전부를 채운다 — 값은 Cloud STT v2 supported-languages
#   표 모양(지역 포함 BCP-47: ja-JP · cmn-Hans-CN(만다린 간체) · fr-FR · vi-VN) 이고 Gemini Live AudioTranscriptionConfig.language_codes 도 같은 BCP-47 을 받는다.
_STT_LANGUAGE_ALIASES: dict[str, str] = {
    "en": "en-US", "ko": "ko-KR",
    "ja": "ja-JP", "zh": "cmn-Hans-CN", "fr": "fr-FR", "vi": "vi-VN",
}
# 모양 검사(BCP-47 근사): 언어[-문자]**-지역**. 통과한 값은 그대로 벤더에 넘긴다.
# ⭐ **지역이 없으면 거절한다.** v2 지원 표의 코드는 지역까지 있다(en-US · ko-KR · cmn-Hans-CN).
#   짧은 코드("en" 같은)를 그대로 통과시키면 "설정값을 그대로 STT 에 꽂았는데 조용히
#   안 들리는" 지금 결함이 형태만 바꿔 되살아난다.
_BCP47_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z]{4})?-([A-Za-z]{2}|\d{3})$")


# ⛔⛔ **이 함수는 캐스케이드 전용이 아니다 — 지울 때 같이 지우지 마라**(2026-08-20).
#   ⭐⭐ C14-b(2026-09-23) 갱신 — 살아있는 호출부는 **라이브 통화 하나뿐**이다
#   (`call_session._input_language_codes` → Gemini Live 입력 전사 언어 힌트, call_session.py:638
#   근방). ⛔ "발음 챌린지가 쓴다"는 옛 서술은 **부정확했다** — 발음 챌린지(`stt_session.py`
#   → `/pron/stt/ws`)는 v1 `make_stt_stream`(문서상 위 "발음 챌린지 서버 STT" 절, Google
#   전용·언어 코드 정규화 없음)만 쓰고 이 함수를 호출하지 않는다(grep 확인, 0건). 캐스케이드
#   호출부(`make_stt_v2_stream`)는 캐스케이드 엔진 삭제로 죽었지만, 함수 자체가 이 파일
#   전체와 함께 보호 대상이라 코드는 그대로 남아 있다 — 지금은 **미사용 경로**다.
#   ⭐ 라이브가 여기 기댄 이유: 입력 전사를 힌트 없이 열어 뒀더니 짧은 한국어가 다른 언어로
#   찍혔는데(실측 call_id=1097: "다"→`套`, "아주"→`और च`), **캐스케이드가 2026-08-08 에
#   똑같은 결함을 이미 겪고** 이 변환을 만들어 뒀다. 표를 하나 더 만들면 같은 질문에 답이 둘이 된다.
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

