"""TTS 엔진 A/B 스위치 — Chirp3-HD ↔ Gemini-TTS (크레덴셜 0, 가짜 클라이언트).

사장님이 두 엔진을 **번갈아 들으며** 고르신다. 그래서 요구가 "교체"가 아니라 "스위치"다:
env 하나로 왔다갔다 하고, 느리면 1분 안에 되돌린다.

여기서 못박는 것:
  ① **서버 기본은 Gemini-TTS 다**(2026-08-12 사장님 결정 "지금 좋아 잘돼"). 앱이 붙으면
     이 값이 곧 실서비스 소리라, 기본이 무엇인지는 회귀로 계속 못박혀 있어야 한다.
     ⚠ Chirp 를 고르면 예전 동작 그대로다(모델 지정도, 감정 지시도 없다).
  ② gemini 를 켜면 **모델 id 가 요청에 실리고** 감정 지시(prompt)가 함께 간다
  ③ ⭐ gemini 가 실패하면 **Chirp3-HD 로 폴백하되 조용히 하지 않는다**(WARNING + report)
     — 조용한 폴백은 "제미나이가 느리다"를 "제미나이인 줄 알았는데 Chirp 였다"로 만든다
  ④ 이미 소리가 나가던 중 끊기면 폴백하지 않는다(같은 문장을 두 번 말하게 된다)
  ⑤ ⛔ **엔진마다 음성명 형식이 다르다** — Chirp 'ko-KR-Chirp3-HD-Sulafat' / Gemini 'Sulafat'.
     섞으면 400 "Gemini models cannot be used with non-Gemini voices." 또는 404 다.
     실사격에서만 드러난 결함이라, 테스트가 없으면 다음 리팩터에 "통일하자"며 되돌아온다.
"""

import logging

import pytest

from core import tts

texttospeech = pytest.importorskip("google.cloud.texttospeech")

_FRAME = b"\x01\x00" * 240


class _FakeClient:
    """streaming_synthesize 대역. 받은 요청을 기록하고, 정해진 대로 성공/실패한다."""

    def __init__(self, fail_for: str | None = None, fail_midway: bool = False) -> None:
        self.requests: list = []
        self._fail_for = fail_for          # 이 model_name 이면 실패
        self._fail_midway = fail_midway

    async def streaming_synthesize(self, requests=None):
        collected = [r async for r in requests]
        self.requests.append(collected)
        model = collected[0].streaming_config.voice.model_name
        fail = self._fail_for is not None and model == self._fail_for

        async def _gen():
            if fail and not self._fail_midway:
                raise RuntimeError("400 Unsupported model")
            yield _resp(_FRAME)
            if fail and self._fail_midway:
                raise RuntimeError("스트림 중단")
            yield _resp(_FRAME)

        return _gen()


class _resp:
    def __init__(self, audio: bytes) -> None:
        self.audio_content = audio


def _voice_of(client: _FakeClient, call: int = 0):
    return client.requests[call][0].streaming_config.voice


def _input_of(client: _FakeClient, call: int = 0):
    return client.requests[call][1].input


async def _drain(stream) -> bytes:
    out = b""
    async for chunk in stream:
        out += chunk
    return out


@pytest.fixture
def rig(monkeypatch):
    def _install(client, **settings_update):
        monkeypatch.setattr(tts, "_client", lambda: client)
        for key, value in settings_update.items():
            monkeypatch.setattr(tts.settings, key, value)
        return client
    monkeypatch.setattr(tts, "_FIRST_BYTES_LOGGED", set())
    return _install


@pytest.mark.asyncio
async def test_choosing_chirp_keeps_the_old_behaviour(rig):
    """Chirp 를 고르면 예전 동작 그대로 — 모델 지정도, 감정 지시도 없다.

    ⚠ 이건 **기본값 검증이 아니다**(엔진을 명시로 넘긴다). 기본값은 아래 회귀가 본다 —
      2026-08-12 까지 이 시험의 이름이 `default` 여서 기본값이 검증되는 줄 알았다.
    """
    client = rig(_FakeClient(), CASCADE_TTS_ENGINE="chirp3-hd")
    report: dict = {}
    audio = await _drain(await tts.synthesize_stream("안녕하세요", report=report))
    assert audio == _FRAME * 2
    assert _voice_of(client).model_name == ""      # Chirp3-HD 는 model_name 을 안 쓴다
    assert _input_of(client).prompt == ""          # 감정 지시는 Gemini 전용
    assert report["engine"] == tts.CHIRP3_ENGINE


def test_the_server_default_is_gemini():
    """⭐ **서버 기본값 = Gemini-TTS.** 클라가 아무 말 안 하면 이 소리가 나간다.

    ⛔ 값 하나만 보지 않는다 — 그 기본을 골랐을 때 **실제로 Gemini 로 도는지**까지 본다
      (기본값만 바꾸고 성질 표를 안 옮기면 조용히 Chirp 이 난다).
    ⚠ 되돌리려면 env 로 "chirp3-hd" 를 넣으면 끝이다 — 쿼터(429) 위험 때문에 그 길은 열어 둔다.
    """
    import domains.learning.realtime.cascade_session as cs
    from core.config import settings

    assert settings.CASCADE_TTS_ENGINE == "gemini-tts"

    class _Sink:
        async def send_event(self, event: dict) -> None:
            return None

        async def send_audio(self, frame: bytes) -> None:
            return None

        async def receive(self):
            raise AssertionError

    session = cs.CascadeSession(_Sink())
    assert session._tts_engine == "gemini-tts", "기본값이 세션에 안 실린다"
    assert session._profile().google_engine == tts.GEMINI_ENGINE
    assert session._profile().takes_style is True, "감정 지시가 안 나간다"
    assert session._tts_vendor() == "gemini-2.5-flash-tts"


@pytest.mark.asyncio
async def test_gemini_engine_sends_model_and_style_prompt(rig):
    """② 켜면 모델 id·감정 지시가 실리고, 음성명은 **맨이름**으로 나간다."""
    client = rig(
        _FakeClient(),
        CASCADE_TTS_ENGINE="gemini-tts",
        CASCADE_TTS_GEMINI_MODEL="gemini-2.5-flash-tts",
        CASCADE_TTS_STYLE_PROMPT="밝게 천천히",
    )
    report: dict = {}
    await _drain(await tts.synthesize_stream("안녕하세요", voice="Fenrir", report=report))
    voice = _voice_of(client)
    assert voice.model_name == "gemini-2.5-flash-tts"
    # ⛔ Gemini 는 **맨이름**만 받는다. 접두어를 붙이면 400 "Gemini models cannot be used
    #   with non-Gemini voices." 다(2026-08-07 실사격 확인). 로스터는 공유하지만 형식이 다르다.
    assert voice.name == "Fenrir"
    assert voice.language_code == "ko-KR"
    assert _input_of(client).prompt == "밝게 천천히"
    assert report["engine"] == "gemini-2.5-flash-tts"


@pytest.mark.asyncio
async def test_gemini_failure_falls_back_loudly(rig, caplog):
    """③ ⭐ 실패하면 Chirp3-HD 로 폴백하되 **조용히 하지 않는다**."""
    client = rig(
        _FakeClient(fail_for="gemini-2.5-flash-tts"),
        CASCADE_TTS_ENGINE="gemini-tts",
        CASCADE_TTS_GEMINI_MODEL="gemini-2.5-flash-tts",
    )
    report: dict = {}
    with caplog.at_level(logging.WARNING):
        audio = await _drain(await tts.synthesize_stream("안녕하세요", report=report))
    assert audio == _FRAME * 2                       # 소리는 났다(사장님이 벙어리를 만나지 않는다)
    assert report["fallback_from"] == "gemini-2.5-flash-tts"
    assert report["engine"] == tts.CHIRP3_ENGINE     # 원가·로그는 **실제로 낸 엔진**으로
    assert any("폴백" in r.getMessage() for r in caplog.records), caplog.text
    second = _voice_of(client, 1)
    assert second.model_name == ""                   # 두 번째 시도는 Chirp3-HD
    # ⭐ 폴백은 이름 형식도 같이 바꿔야 한다 — 맨이름 그대로 보내면 Chirp 이 404 를 낸다.
    assert second.name == "ko-KR-Chirp3-HD-Aoede"
    assert _voice_of(client, 0).name == "Aoede"


@pytest.mark.asyncio
async def test_no_fallback_after_audio_already_started(rig, caplog):
    """④ 이미 소리가 나가던 중 끊기면 폴백하지 않는다 — 같은 문장을 두 번 말하게 된다."""
    client = rig(
        _FakeClient(fail_for="gemini-2.5-flash-tts", fail_midway=True),
        CASCADE_TTS_ENGINE="gemini-tts",
        CASCADE_TTS_GEMINI_MODEL="gemini-2.5-flash-tts",
    )
    report: dict = {}
    with caplog.at_level(logging.WARNING):
        audio = await _drain(await tts.synthesize_stream("안녕하세요", report=report))
    assert audio == _FRAME                            # 나간 데까지만
    assert "fallback_from" not in report
    assert len(client.requests) == 1                  # 재시도 없음
    assert report["engine"] == "gemini-2.5-flash-tts"


# --------------------------------------------------------------------------- #
# 말하기 배속은 **엔진마다 다르다** (2026-08-10)
#   실측: Chirp en 14.4자per초 vs Gemini en 11.1 = 약 1.3배 차이.
#   공통 값 하나를 올리면 **Chirp 까지 빨라진다** — Chirp 은 이미 충분하다
#   (사장님: "빠르게 잘 나온다"). 올릴 곳은 Gemini 뿐이라 레버를 나눈다.
# --------------------------------------------------------------------------- #
def _session_with(engine: str):
    import domains.learning.realtime.cascade_session as cs

    class _Sink:
        async def send_event(self, event: dict) -> None:
            return None

        async def send_audio(self, frame: bytes) -> None:
            return None

        async def receive(self):
            raise AssertionError("쓰지 않는다")

    session = cs.CascadeSession(_Sink())
    session._tts_engine = engine
    return session


def test_gemini_rate_is_separate_from_the_common_one(monkeypatch):
    """⭐ Gemini 만 올릴 수 있어야 한다 — Chirp 은 공통 값을 그대로 쓴다."""
    import domains.learning.realtime.cascade_session as cs

    monkeypatch.setattr(cs.settings, "CASCADE_TTS_SPEAKING_RATE", 1.0)
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_SPEAKING_RATE_GEMINI", 1.3)
    assert _session_with("gemini-tts")._speaking_rate() == pytest.approx(1.3)
    assert _session_with("gemini-batch")._speaking_rate() == pytest.approx(1.3)
    assert _session_with("chirp3-hd")._speaking_rate() == pytest.approx(1.0)
    assert _session_with("")._speaking_rate() == pytest.approx(1.0)   # 서버 기본값(Chirp)


def test_client_choice_still_wins(monkeypatch):
    """데모 화면에서 고른 값이 있으면 그게 우선이다(A/B 하려고 만든 통로)."""
    import domains.learning.realtime.cascade_session as cs

    monkeypatch.setattr(cs.settings, "CASCADE_TTS_SPEAKING_RATE_GEMINI", 1.3)
    session = _session_with("gemini-tts")
    session._tts_rate = 0.9
    assert session._speaking_rate() == pytest.approx(0.9)


def test_rates_start_unchanged_until_measured():
    """⛔ **레버만 달았지 값은 안 올렸다.**

    앞 커밋(구간 침묵 정리)이 체감을 바꾸므로 **먼저 재고 나서** 정한다. 둘을 한꺼번에
    올리면 어느 쪽이 얼마를 기여했는지 못 가린다.
    """
    from core.config import settings as live

    assert live.CASCADE_TTS_SPEAKING_RATE == 1.0
    assert live.CASCADE_TTS_SPEAKING_RATE_GEMINI == 1.0
    # 문서 범위 [0.25, 2.0] 안에 있어야 한다(벗어나면 요청이 거절된다)
    assert 0.25 <= live.CASCADE_TTS_SPEAKING_RATE_GEMINI <= 2.0


# ── ⑥ 폴백은 **통화 요약에도** 남는다(2026-08-12) ──────────────────────────
def test_a_quota_fallback_is_counted_in_the_call_summary():
    """⛔ 폴백이 나면 **통화 중에 목소리가 바뀐다.** 사장님은 그걸 소리로만 아신다.

    지금까지는 그 순간의 WARNING 한 줄뿐이라, 통화가 끝난 뒤 "그 통화에서 몇 번 바뀌었나"를
    답할 수 없었다. Gemini 를 **기본**으로 쓰기로 한 이상(쿼터가 있는 유일한 엔진) 이 숫자가
    요약에 있어야 한다.
    """
    from domains.learning.realtime.cascade_usage import CascadeUsage, format_usage_line

    usage = CascadeUsage()
    usage.record_tts("안녕하세요", vendor="gemini-2.5-flash-tts")
    usage.record_tts_audio(48000)
    assert usage.summary()["vendors"]["tts"]["fallbacks"] == 0

    usage.record_tts("", vendor="gemini-2.5-flash-tts", failed=True)
    usage.record_tts_fallback()
    usage.record_tts_fallback()
    summary = usage.summary()
    assert summary["vendors"]["tts"]["fallbacks"] == 2
    assert "tts_fallbacks=2" in format_usage_line(summary), format_usage_line(summary)


@pytest.mark.asyncio
async def test_the_session_counts_the_fallback_where_it_actually_happens(monkeypatch):
    """⭐ 세는 자리는 **폴백을 실제로 감지하는 곳**이어야 한다(따로 세면 갈린다)."""
    import domains.learning.realtime.cascade_session as cs

    class _Sink:
        async def send_event(self, event: dict) -> None:
            return None

        async def send_audio(self, frame: bytes) -> None:
            return None

        async def receive(self):
            raise AssertionError

    async def _stream(text, **kwargs):
        report = kwargs.get("report")
        if report is not None:
            report.update({"engine": tts.CHIRP3_ENGINE,
                           "fallback_from": "gemini-2.5-flash-tts", "quota": True})

        async def _gen():
            yield b"\x01\x00" * 240
        return _gen()

    monkeypatch.setattr(cs.tts, "synthesize_stream", _stream)
    session = cs.CascadeSession(_Sink())
    await session.beaver.begin()
    await session._speak_one("안녕하세요", "ko")
    assert session.usage.summary()["vendors"]["tts"]["fallbacks"] == 1
