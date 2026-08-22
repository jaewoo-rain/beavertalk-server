"""STT 다중 언어 회귀 — **모국어로 말하면 전사가 통째로 사라지던 결함**.

2026-08-08 실통화(09:45~09:47):
    u11 speech_ms=1194 text=''   u12 946 text=''   u13 1025 text=''
    u14 1206 text=''             u15 1200 text=''
5회 연속·약 36초 동안 전사 0. VAD 는 소리를 들었다(speech_ms 가 1초씩 있다). 사장님 확인:
**"11~15는 영어로 말했다."** 같은 통화의 u10 은 영어를 '베이킹 센' 으로 억지 음차했고,
한국어 발화(u3~u7)는 정확했다.

원인: `language_codes=[settings.STT_V2_LANGUAGE or settings.STT_LANGUAGE]` — 필드는 리스트인데
우리가 **한 개(ko-KR)** 만 넣었다. 우리 사용자는 외국인 학습자다. 그들은 모국어로 묻고
한국어로 따라 말한다. **한쪽만 들으면 다른 쪽은 사라진다.**

1차 자료(https://cloud.google.com/speech-to-text/v2/docs/multiple-languages, 2026-08-08 확인):
  · "You can only use the alternative languages feature with the long, short, and telephony
     models."                                   → 우리 모델 `long` = 지원
  · "You can list up to three languages for automatic language recognition."
  · "Specifying multiple languages is only available in the ... global region and the us and
     eu multi-regions."                          → 우리 위치 `global` = 지원
  · "constrain the language list to the bare minimum needed as a best practice"

여기서 고정하는 성질:
  ① 데모 통화는 **학습 언어 + 모국어**를 같이 듣는다
  ② 짧은 코드(en)는 STT 코드가 아니다 — 지역까지 붙여 보낸다(en-US)
  ③ 문서 상한 3개를 넘기지 않는다
  ④ 이상한 코드가 통화를 죽이지 않는다(R5) — 걸러내고, 그래도 실패하면 한 언어로 강등
"""

import asyncio

import pytest

import core.stt as stt_mod
from core.stt import SPEECH_BEGIN, STREAM_ERROR, RollingSttV2Stream, SttV2Event
from domains.learning.realtime.cascade_session import CascadeInbound, CascadeSession


# ── ② 정규화 ────────────────────────────────────────────────────────────────
def test_short_codes_become_full_bcp47():
    """`en` 은 STT 코드가 아니다 — 지역까지 있어야 한다(1차 자료: v2 supported-languages)."""
    assert stt_mod.normalize_language_codes(["ko", "en"]) == ["ko-KR", "en-US"]


def test_case_and_separator_are_normalized():
    """클라·env 가 `KO_kr` 처럼 보내도 같은 언어로 본다(중복도 지운다)."""
    assert stt_mod.normalize_language_codes(["ko-KR", "ko", "KO_kr", "en-us"]) \
        == ["ko-KR", "en-US"]


def test_unverified_short_codes_are_dropped_not_guessed():
    """⛔ **모르는 짧은 코드는 추측하지 않는다.**

    틀린 코드를 넣으면 그 언어는 조용히 안 들린다(지금 결함과 같은 실패). 확인한 매핑만
    쓰고 나머지는 버린다 — 버려도 동작은 지금과 같다(학습 언어는 그대로 들린다).
    """
    assert stt_mod.normalize_language_codes(["ko", "vi", "english", ""]) == ["ko-KR"]


def test_full_tags_pass_through():
    """이미 완전한 태그는 그대로 벤더에 넘긴다(우리가 아는 언어만 지원하지 않는다)."""
    assert stt_mod.normalize_language_codes(["ja-JP", "cmn-Hans-CN"]) == ["ja-JP", "cmn-Hans-CN"]


def test_vendor_limit_of_three_is_enforced():
    """문서 상한 3개. 넘겨 보내면 요청 자체가 거절될 수 있다."""
    codes = stt_mod.normalize_language_codes(["ko-KR", "en-US", "ja-JP", "fr-FR"])
    assert codes == ["ko-KR", "en-US", "ja-JP"]
    assert len(codes) <= stt_mod.STT_V2_MAX_LANGUAGES


def test_empty_falls_back_instead_of_sending_nothing():
    """⛔ 언어 코드가 비면 스트림이 400 으로 죽고, 그건 **통화가 죽는다**는 뜻이다(R5)."""
    assert stt_mod.normalize_language_codes([], fallback="ko-KR") == ["ko-KR"]
    assert stt_mod.normalize_language_codes(["english"], fallback="ko") == ["ko-KR"]


# ── ① 데모 경로가 두 언어를 싣는다 ──────────────────────────────────────────
class _Sink:
    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def receive(self) -> CascadeInbound:
        await asyncio.sleep(3600)
        raise AssertionError("이 테스트는 receive 를 쓰지 않는다")


def test_call_listens_to_both_target_and_native_language(monkeypatch):
    """⭐ 학습 언어(ko)와 모국어(en)를 **같이** 듣는다 — 결함의 본체다."""
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TTS_TARGET_LANGUAGE", "ko")
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TTS_LANGUAGE", "en")
    session = CascadeSession(_Sink())
    assert stt_mod.normalize_language_codes(session._stt_language_codes()) == ["ko-KR", "en-US"]


def test_native_language_is_env_switchable(monkeypatch):
    """데모는 env 로 모국어를 바꿔 실험할 수 있어야 한다."""
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TTS_TARGET_LANGUAGE", "ko")
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TTS_LANGUAGE", "ja-JP")
    session = CascadeSession(_Sink())
    assert stt_mod.normalize_language_codes(session._stt_language_codes()) == ["ko-KR", "ja-JP"]


# ── ④ 실패해도 통화가 죽지 않는다 ───────────────────────────────────────────
class _PickyStream:
    """언어를 여러 개 주면 개시가 실패하는 벤더 흉내(설정 오류 재현)."""

    def __init__(self, language_codes, opened: list) -> None:
        self._codes = list(language_codes)
        self._opened = opened

    async def start(self) -> None:
        self._opened.append(list(self._codes))
        if len(self._codes) > 1:
            raise RuntimeError("INVALID_ARGUMENT: language_codes")

    async def events(self):
        yield SttV2Event(kind=SPEECH_BEGIN, offset_ms=0)
        await asyncio.sleep(3600)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_multi_language_failure_degrades_instead_of_killing_the_call():
    """⛔ 다중 언어가 안 먹히면 **한 언어로 내려앉되 통화는 산다**(R5).

    지금까지는 언어가 하나라 실패할 일이 없었다. 여러 개를 넣는 순간 실패 경로가 생긴다 —
    거기서 통화가 통째로 죽으면 결함을 고치다 더 큰 결함을 만드는 것이다.
    """
    opened: list = []
    stream = RollingSttV2Stream(
        lambda codes: _PickyStream(codes, opened), 16000,
        language_codes=["ko-KR", "en-US"],
    )
    events = stream.events()
    first = await asyncio.wait_for(events.__anext__(), timeout=2)
    assert first.kind == SPEECH_BEGIN, "강등 뒤에도 이벤트가 나와야 한다"
    assert opened == [["ko-KR", "en-US"], ["ko-KR"]], opened
    assert stream.language_codes == ["ko-KR"], "강등 결과가 남아 있어야 롤오버도 같은 언어로 연다"
    await events.aclose()


@pytest.mark.asyncio
async def test_single_language_failure_still_reports_error():
    """⚠ 강등은 **여러 개일 때만**이다. 한 개로도 안 열리면 그건 진짜 고장이라 알려야 한다."""

    class _Dead:
        def __init__(self, codes) -> None:
            self._codes = codes

        async def start(self) -> None:
            raise RuntimeError("boom")

        async def events(self):
            return
            yield

        async def close(self) -> None:
            return None

    stream = RollingSttV2Stream(lambda codes: _Dead(codes), 16000, language_codes=["ko-KR"])
    kinds = []
    async for event in stream.events():
        kinds.append(event.kind)
        if event.kind == STREAM_ERROR:
            break
    assert kinds[-1] == STREAM_ERROR


def test_degrade_is_idempotent():
    """이미 한 개면 더 내려갈 곳이 없다(무한 강등·로그 폭발 방지)."""
    stream = RollingSttV2Stream(lambda codes: None, 16000, language_codes=["ko-KR", "en-US"])
    assert stream._degrade_languages("테스트") is True
    assert stream._degrade_languages("테스트") is False
    assert stream.language_codes == ["ko-KR"]


# ── ④ 라이브 통화 입력 전사 언어 힌트 (2026-08-20) ──────────────────────────
# ⛔⛔ **여기부터는 캐스케이드가 아니라 라이브 회귀다.** 파일 이름 때문에 캐스케이드
#   테스트로 보이지만 아니다 — 캐스케이드 코드를 정리할 때 **이 절은 지우지 마라**
#   (라이브 테스트 파일로 옮기는 건 좋다).
#   기록: docs/20260813_0040_캐스케이드-데모잔재-정리목록.md §2-b
# 같은 결함이 **라이브 쪽에도** 있었다. 캐스케이드는 2026-08-08 에 고쳤는데(위),
# Gemini Live 의 입력 전사는 여전히 언어 힌트 없이 열려 있었다.
# 실측 call_id=1097(ko 학습 / en 모국어) — 학습자가 한국어를 따라 말했는데:
#     "피우다"→`フィウダ`  "다"→`套`  "아주"→`और च`  짧은 응답→`für 10`·`kumite`·`Sí.`
# ⛔ 이 전사는 CallRawData 로 저장돼 이어하기 요약·통화후 문장 추출·증거 인용 검증이 읽는다.
def test_live_call_hints_both_target_and_native_language():
    """라이브도 학습 언어 + 모국어를 같이 듣는다 — 순서는 학습 언어 먼저."""
    from domains.learning.realtime.call_session import _input_language_codes

    assert _input_language_codes("ko", "en") == ["ko-KR", "en-US"]


def test_live_call_dedupes_when_target_equals_native():
    """학습 언어와 모국어가 같으면 한 개다(중복 힌트는 의미가 없다)."""
    from domains.learning.realtime.call_session import _input_language_codes

    assert _input_language_codes("ko", "ko") == ["ko-KR"]


def test_live_call_gives_up_entirely_when_any_code_is_unmapped():
    """⛔⛔ **부분 힌트는 무힌트보다 나쁘다** — 하나라도 못 만들면 통째로 포기한다.

    `ja` 는 아직 검증된 매핑이 없다(_STT_LANGUAGE_ALIASES). 여기서 모국어 `en-US` 만
    남겨 보내면, **일본어 발화를 영어로 알아들으라고 시키는** 꼴이 된다. 빈 목록은
    "힌트 없음"이고 그건 종전 동작(자동 감지)이다 — 안전한 쪽으로 떨어진다.
    """
    from domains.learning.realtime.call_session import _input_language_codes

    assert _input_language_codes("ja", "en") == []
    assert _input_language_codes("ko", "vi") == []
