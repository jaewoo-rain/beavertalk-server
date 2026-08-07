"""TTS 엔진 A/B 스위치 — Chirp3-HD ↔ Gemini-TTS (크레덴셜 0, 가짜 클라이언트).

사장님이 두 엔진을 **번갈아 들으며** 고르신다. 그래서 요구가 "교체"가 아니라 "스위치"다:
env 하나로 왔다갔다 하고, 느리면 1분 안에 되돌린다.

여기서 못박는 것:
  ① 기본은 Chirp3-HD 다 — 스위치를 안 켜면 지금 동작 그대로(감정 지시도 안 보낸다)
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
async def test_default_stays_on_chirp3(rig):
    """① 스위치를 안 켜면 지금 동작 그대로 — 모델 지정도, 감정 지시도 없다."""
    client = rig(_FakeClient(), CASCADE_TTS_ENGINE="chirp3-hd")
    report: dict = {}
    audio = await _drain(await tts.synthesize_stream("안녕하세요", report=report))
    assert audio == _FRAME * 2
    assert _voice_of(client).model_name == ""      # Chirp3-HD 는 model_name 을 안 쓴다
    assert _input_of(client).prompt == ""          # 감정 지시는 Gemini 전용
    assert report["engine"] == tts.CHIRP3_ENGINE


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
