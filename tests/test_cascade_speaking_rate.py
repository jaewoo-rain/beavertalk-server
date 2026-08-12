"""말하기 배속은 **언어별**이다 — 하나의 값으로는 둘을 못 맞춘다(2026-08-10).

⛔ 단위 함정부터: `자per초` 는 **언어 간 직접 비교가 안 된다.** 한국어 1글자(음절 덩어리)가
영어 3~4글자만큼 소리를 낸다. 반드시 같은 언어끼리 비교해야 한다.

같은 언어끼리 본 실측:
    한국어  Live **7.7**  vs Gemini 6.2~7.4   → 거의 맞았다
    영어    Chirp **19.6** vs Gemini 12.0     → Gemini 가 1.6배 느리다
하나의 rate 를 1.6 으로 올리면 영어는 맞지만 **한국어가 11.8** 이 되어 Live 를 한참 넘긴다.
한국어는 **학습자가 따라 말하는 부분**이라 빨라지면 안 된다.

여기서 고정하는 성질:
  ① 기본값은 전부 1.0 → **이번 배포로 소리가 안 바뀐다**
  ② 한국어 구간과 영어 구간이 **다른** rate 로 나간다
  ③ 범위 [0.25, 2.0] 밖은 거절(proto 원문)
  ④ 구간을 더 쪼개지 않는다 — 429 상한이 10이다
"""

import pytest

import domains.learning.realtime.cascade_session as cs


class _Sink:
    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def receive(self):
        raise AssertionError("쓰지 않는다")


def _session(monkeypatch, by_lang: str = "") -> cs.CascadeSession:
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_SPEAKING_RATE_BY_LANG", by_lang)
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_LANGUAGE", "en")
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_TARGET_LANGUAGE", "ko")
    return cs.CascadeSession(_Sink())


# ── ① 기본은 무변경 ────────────────────────────────────────────────────────
def test_defaults_change_nothing():
    """⛔ 값은 사장님이 **귀로** 찾으실 것이다 — 오늘 '1.3 이겠지'로 두 번 어긋났다."""
    from core.config import settings as live

    assert live.CASCADE_TTS_SPEAKING_RATE == 1.0
    assert live.CASCADE_TTS_SPEAKING_RATE_GEMINI == 1.0
    assert live.CASCADE_TTS_SPEAKING_RATE_BY_LANG == ""


def test_no_language_setting_falls_back_to_the_engine_default(monkeypatch):
    session = _session(monkeypatch)
    session._tts_engine = "gemini-tts"
    assert session._speaking_rate("ko") == pytest.approx(1.0)
    assert session._speaking_rate("en") == pytest.approx(1.0)


# ── ② 언어별로 다르게 나간다 ───────────────────────────────────────────────
def test_korean_and_english_segments_get_different_rates(monkeypatch):
    """⭐ 이 기능의 요점 — 영어만 올리고 한국어는 그대로 둘 수 있어야 한다."""
    session = _session(monkeypatch, by_lang="en:1.6,ko:1.0")
    assert session._speaking_rate("en") == pytest.approx(1.6)
    assert session._speaking_rate("ko") == pytest.approx(1.0)
    assert session._speaking_rate("ja") == pytest.approx(1.0)   # 미지정 언어는 폴백


def test_language_value_beats_the_client_wide_value(monkeypatch):
    """언어별 값이 공통 값보다 구체적이다 — 구체적인 쪽이 이긴다."""
    session = _session(monkeypatch, by_lang="ko:0.9")
    session._tts_rate = 1.5                     # 데모 화면의 기존 통로(공통)
    assert session._speaking_rate("ko") == pytest.approx(0.9)
    assert session._speaking_rate("en") == pytest.approx(1.5)


def test_client_can_set_rates_per_language(monkeypatch):
    """데모 화면은 서버의 언어 코드를 모른다 — '설명/한국어'로 보내면 서버가 얹는다."""
    session = _session(monkeypatch)
    session._apply_tts_choice({"speakingRateNative": 1.45, "speakingRateTarget": 1.05})
    assert session._speaking_rate("en") == pytest.approx(1.45)
    assert session._speaking_rate("ko") == pytest.approx(1.05)


# ── ③ 범위 밖 거절 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", ["en:2.5", "en:0.1", "en:빠르게", "en:", ":1.4"])
def test_out_of_range_or_garbage_is_dropped(monkeypatch, raw):
    """⛔ 조용히 넣으면 요청이 통째로 거절된다 — 버리고 경고한다(R5)."""
    session = _session(monkeypatch, by_lang=raw)
    assert session._tts_rate_by_lang == {}


def test_client_out_of_range_is_rejected(monkeypatch):
    session = _session(monkeypatch)
    session._apply_tts_choice({"speakingRateNative": 9.0, "speakingRateTarget": "빠르게"})
    assert session._tts_rate_by_lang == {}


# ── ④ 구간을 더 쪼개지 않는다 + 로그 ───────────────────────────────────────
def test_rate_does_not_add_new_segments(monkeypatch):
    """⛔ 429 상한이 10이다. **언어 구간 분할은 마커가 정한다** — rate 는 거기 얹힐 뿐이다.

    `_speaking_rate` 는 구간을 만들지 않고 **주어진 구간의 언어를 보고 값만 고른다**.
    (구간 분할이 rate 를 참조하면 같은 언어가 더 쪼개져 호출이 는다.)
    """
    import inspect

    src = inspect.getsource(cs.CascadeSession._speak)
    assert "_speaking_rate" not in src and "_note_rate" not in src, src


def test_reply_log_shows_the_rate_that_actually_went_out(monkeypatch):
    """세션 값만 찍으면 구간별로 달라진 걸 확인할 방법이 없다."""
    session = _session(monkeypatch, by_lang="en:1.6,ko:1.0")
    assert session._rate_log() == "배속=서버값"
    session._note_rate("en")
    session._note_rate("ko")
    assert session._rate_log() == "배속=[en:1.60 ko:1.00]"
