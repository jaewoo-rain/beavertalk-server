"""OpenAI TTS(`/v1/audio/speech`) — **배관을 새로 만들지 않는다**(2026-08-10).

1차 자료(https://developers.openai.com/api/docs/guides/text-to-speech, 확인일 08-10):
  · `pcm` = "raw samples in **24kHz (16-bit signed, low-endian)**, without the header"
    → `CASCADE_TTS_SAMPLE_RATE`(24000)와 같다. **변환이 필요 없다.**
  · "realtime audio streaming using **chunk transfer encoding**" → 조각 즉시 송출(H1).
  · ⛔ `speed` 파라미터는 **문서에 없다.** 같은 문서가 드는 "Speed of speech" 는
    `instructions` 로 **말로 부탁하는** 항목이다 — 그 길은 우리가 이미 막았다(스타일 프롬프트
    속도 어휘 금지 회귀. "또박또박" 한 낱말이 한국어 속도의 절반을 먹었다).

여기서 고정하는 성질:
  ① 24k PCM 조각이 그대로 흐른다(변환·헤더 없음)
  ② 키가 없으면 **그 엔진만 거절** + 서버 기본값(조용히 안 바꾼다)
  ③ 감정 태그가 `instructions` 로 실려 나간다
  ④ 마커 분할은 **env 로 켜고 끈다**(기본은 지금처럼 분할 — 안 나눈 발음은 미확인)
⛔ 실 API 를 부르지 않는다(과금). 계약만 페이크로 고정한다.
"""

import pytest

import core.openai_tts as oai_tts
import domains.learning.realtime.cascade_session as cs


class _Sink:
    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def receive(self):
        raise AssertionError("쓰지 않는다")


class _FakeResp:
    status_code = 200

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = frames

    async def aiter_bytes(self):
        for frame in self._frames:
            yield frame

    async def aread(self) -> bytes:
        return b""


def _fake_client(frames: list[bytes], seen: dict):
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, **kwargs):
            seen.update(kwargs.get("json") or {})
            resp = _FakeResp(frames)

            class _Ctx:
                async def __aenter__(self_inner):
                    return resp

                async def __aexit__(self_inner, *a):
                    return False

            return _Ctx()

    return lambda **kw: _Client()


# ── ① 24k PCM 이 그대로 흐른다 ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_pcm_chunks_flow_through_unchanged(monkeypatch):
    """벤더 조각을 **손대지 않고** 흘린다 — 규약(24k/16bit/mono)이 이미 같기 때문이다."""
    frames = [b"\x11\x22" * 240, b"\x33\x44" * 240]
    seen: dict = {}
    monkeypatch.setattr(oai_tts.settings, "GPT_API_KEY", "x")
    monkeypatch.setattr(oai_tts.httpx, "AsyncClient", _fake_client(frames, seen))
    report: dict = {}
    got = [c async for c in oai_tts.synthesize_stream("안녕하세요", report=report)]
    assert got == frames
    assert seen["response_format"] == "pcm", "이게 아니면 변환 배관이 필요해진다"
    assert seen["model"] == "gpt-4o-mini-tts"
    assert report["engine"] == "openai-gpt-4o-mini-tts"
    assert report["ttfb_ms"] >= 0


@pytest.mark.asyncio
async def test_no_key_yields_nothing_and_never_raises(monkeypatch):
    """R5 — 키가 없다고 통화가 죽지 않는다."""
    monkeypatch.setattr(oai_tts.settings, "GPT_API_KEY", "")
    assert [c async for c in oai_tts.synthesize_stream("안녕")] == []


@pytest.mark.asyncio
async def test_vendor_error_is_loud_but_graceful(monkeypatch, caplog):
    """⛔ 조용히 죽지 않는다 — 어느 모델이 왜 거절됐는지 로그로 갈린다."""
    import logging

    class _Bad(_FakeResp):
        status_code = 400

    def _client(**kw):
        class _C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def stream(self, *a, **kwargs):
                class _Ctx:
                    async def __aenter__(self_inner):
                        return _Bad([])

                    async def __aexit__(self_inner, *a):
                        return False

                return _Ctx()

        return _C()

    monkeypatch.setattr(oai_tts.settings, "GPT_API_KEY", "x")
    monkeypatch.setattr(oai_tts.httpx, "AsyncClient", _client)
    with caplog.at_level(logging.WARNING):
        assert [c async for c in oai_tts.synthesize_stream("안녕")] == []
    assert any("openai-tts 실패" in r.getMessage() for r in caplog.records), caplog.text


# ── ② 키 없으면 그 엔진만 거절 ─────────────────────────────────────────────
def test_engine_is_rejected_without_a_key(monkeypatch, caplog):
    """⛔ 조용히 다른 엔진으로 바꾸면 그 소리를 OpenAI 로 착각하시게 된다."""
    import logging

    monkeypatch.setattr(cs.settings, "GPT_API_KEY", "")
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_ENGINE", "chirp3-hd")
    session = cs.CascadeSession(_Sink())
    with caplog.at_level(logging.WARNING):
        session._apply_tts_choice({"ttsEngine": "openai-tts"})
    assert session._tts_engine == "chirp3-hd"
    assert any("엔진 거절" in r.getMessage() for r in caplog.records), caplog.text


def test_engine_is_accepted_with_a_key(monkeypatch):
    monkeypatch.setattr(cs.settings, "GPT_API_KEY", "x")
    session = cs.CascadeSession(_Sink())
    session._apply_tts_choice({"ttsEngine": "openai-tts"})
    assert session._tts_engine == "openai-tts"
    assert session._tts_vendor() == "openai-gpt-4o-mini-tts"
    # 미측정 엔진이라 선행버퍼를 크게 잡는다(작으면 언더런 — Gemini 에서 겪은 그것)
    assert session.beaver.lead_ms == cs.settings.CASCADE_TTS_LEAD_MS_OPENAI


# ── ③ 감정 태그가 instructions 로 간다 ─────────────────────────────────────
@pytest.mark.asyncio
async def test_emotion_becomes_instructions(monkeypatch):
    """⭐ `instructions` 가 스타일 프롬프트 자리다 — 감정 6종이 그대로 붙는다."""
    seen: list[dict] = []

    async def _stream(text, **kwargs):
        seen.append(kwargs)
        report = kwargs.get("report")
        if report is not None:
            report["engine"] = "openai-gpt-4o-mini-tts"
        yield b"\x00\x01" * 240

    monkeypatch.setattr(cs.openai_tts, "synthesize_stream", _stream)
    session = cs.CascadeSession(_Sink())
    session._tts_engine = "openai-tts"
    session._reply_emotion = "칭찬"
    await session.beaver.begin()
    sent = await session._speak_one("아주 좋아요", "ko")
    assert sent > 0
    assert seen[0]["instructions"] == cs.EMOTION_STYLES["칭찬"]


# ── ④ 마커 분할은 env 로 켜고 끈다 ─────────────────────────────────────────
def test_marker_split_is_on_by_default():
    """⛔ 안 나눴을 때 한국어 발음이 어떻게 되는지 **미확인**이다 — 기본을 바꾸지 않는다."""
    from core.config import settings as live

    assert live.CASCADE_TTS_SINGLE_VOICE_ENGINES == ""
    session = cs.CascadeSession(_Sink())
    session._tts_engine = "openai-tts"
    assert session._single_voice() is False


@pytest.mark.asyncio
async def test_single_voice_engine_sends_one_request_for_a_mixed_sentence(monkeypatch):
    """⭐ 한 음성이 두 언어를 읽으면 **구간을 안 나눈다** — 요청 수와 구간 침묵이 같이 준다."""
    asked: list[str] = []

    async def _stream(text, **kwargs):
        asked.append(text)
        yield b"\x00\x01" * 240

    monkeypatch.setattr(cs.settings, "CASCADE_TTS_SINGLE_VOICE_ENGINES", "openai-tts")
    monkeypatch.setattr(cs.openai_tts, "synthesize_stream", _stream)
    session = cs.CascadeSession(_Sink())
    session._tts_engine = "openai-tts"
    await session.beaver.begin()
    await session._speak("Hello __안녕하세요__ nice")
    assert len(asked) == 1, asked
    assert "__" not in asked[0], "마커가 소리로 나간다"


# ── 원가: 모르면 **미상으로 드러난다** ─────────────────────────────────────
def test_openai_tts_cost_surfaces_as_unknown_not_zero():
    """⛔ 과금 단위가 **오디오 토큰**인데(1차: "Audio … $12.00 / 1M tokens") 초→토큰 환산율이
    문서에 없다. 남의 벤더에 Gemini 의 값(1초=25tok)을 쓰면 그건 추측이다.

    조용히 0원이 되는 것보다 "모른다"가 낫다 — 274044a 가 고친 게 그 종류의 결함이다.
    """
    import domains.learning.service.normalcall_service as svc

    cost, unknown = svc.estimate_cascade_cost_usd({
        "tts": {"vendor": "openai-gpt-4o-mini-tts", "chars": 500, "audio_s": 30.0},
    })
    assert cost == 0.0
    assert unknown == ["tts:openai-gpt-4o-mini-tts"], unknown


# ── 묶음 크기: **요청당 오버헤드가 큰 엔진일수록 크게**(2026-08-11) ─────────
#   OpenAI 가 분기에서 빠져 **Chirp 값 160** 을 물려받고 있었다. Chirp 은 TTFB 165~212ms 라
#   요청이 많아도 견디는데, OpenAI 는 545~953ms 다 — 요청도 많고 왕복도 긴 **최악 조합**.
#   실통화 99자에 요청 6회 / 실측 4회에 벤더 대기 합계 2.90초.
def test_every_tts_choice_has_an_explicit_batch_size():
    """⛔ **엔진을 늘릴 때 여기서 빠지면 안 된다** — 이번 사고가 정확히 그것이다.

    선택지 목록에 있는 값이 표에 없으면 조용히 기본값으로 떨어진다. 그래서 전 선택지를 돈다.
    """
    missing = [c for c in cs._TTS_CHOICES if c not in cs._TTS_BATCH_SETTING]
    assert not missing, f"묶음 크기 표에 없는 선택지: {missing}"
    for choice in cs._TTS_CHOICES:
        assert cs._batch_chars_for(choice) > 0


def test_openai_batches_as_large_as_the_slow_vendors():
    """OpenAI 는 왕복이 Chirp 의 4~5배다 → **큰 묶음** 쪽이어야 한다."""
    assert cs._batch_chars_for("openai-tts") >= cs._batch_chars_for("gemini-tts") * 0.5
    assert cs._batch_chars_for("openai-tts") > cs._batch_chars_for(cs._CHIRP_CHOICE)


def test_unknown_engine_falls_back_loudly(caplog):
    """모르는 엔진은 기본값으로 가되 **조용히 가지 않는다**."""
    import logging

    with caplog.at_level(logging.WARNING):
        assert cs._batch_chars_for("some-new-tts") == cs.settings.CASCADE_TTS_BATCH_CHARS
    assert any("묶음 크기 미지정" in r.getMessage() for r in caplog.records), caplog.text


# ── 첫 문장 단독 송출: **같은 사고의 두 번째**(2026-08-11) ─────────────────
#   조건이 `_gemini_realtime()` 이라 OpenAI 가 Chirp 규칙을 탔다. 실측 첫 배치 오디오가
#   800·1000·1450ms 인데 선행버퍼가 1500ms 라, 버퍼를 못 채우고 바로 바닥나 **끊겼다**.
def test_every_tts_choice_declares_a_first_sentence_rule():
    """⛔ 묶음 크기 표와 **같은 이유**로 전 선택지를 돈다 — 빠지면 남의 규칙을 물려받는다."""
    missing = [c for c in cs._TTS_CHOICES if c not in cs._TTS_SOLO_FIRST]
    assert not missing, f"첫문장 규칙 표에 없는 선택지: {missing}"


def test_slow_vendors_do_not_send_the_first_sentence_alone():
    """왕복이 긴 엔진(OpenAI·Gemini)은 묶어서 낸다."""
    assert cs._solo_first_sentence(cs._OPENAI_TTS_CHOICE) is False
    assert cs._solo_first_sentence(cs.tts.GEMINI_ENGINE) is False


def test_chirp_keeps_sending_the_first_sentence_alone():
    """⛔ Chirp 은 **유지**다 — 왕복이 165~212ms 라 단독 송출이 첫 소리를 앞당긴다."""
    assert cs._solo_first_sentence(cs._CHIRP_CHOICE) is True
    assert cs._solo_first_sentence("") is True, "빈 값 = 서버 기본(Chirp)"


def test_unknown_engine_batches_and_says_so(caplog):
    """모르는 엔진은 **묶는 쪽**(안전)으로 가되 조용히 가지 않는다."""
    import logging

    with caplog.at_level(logging.WARNING):
        assert cs._solo_first_sentence("some-new-tts") is False
    assert any("첫문장 규칙 미지정" in r.getMessage() for r in caplog.records), caplog.text


def test_first_sentence_rule_is_read_from_the_table_not_the_engine_name():
    """⭐ **표로 물어야** 새 엔진이 남의 규칙을 물려받지 않는다.

    ⚠ 소스 문자열로 보면 주석에 걸린다(사고 경위를 주석에 적어 뒀다) — 코드만 본다.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(cs.CascadeSession._run_reply)))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "_solo_first_sentence" in called, "표를 안 묻는다"
    assert "_gemini_realtime" not in attrs, "엔진 이름으로 되돌아갔다"


def test_choice_list_is_the_single_source(monkeypatch):
    """선택지 나열이 흩어지면 또 어딘가에서 빠진다 — 수락 판정도 같은 목록을 쓴다."""
    import inspect

    src = inspect.getsource(cs.CascadeSession._apply_tts_choice)
    assert "_TTS_CHOICES" in src, src[-400:]
