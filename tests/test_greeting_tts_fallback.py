"""첫 인사 벙어리 → 받은 인사 텍스트를 TTS 로 대체(qa 2262·2263 · 첫 소리 6~8초)."""
import json

import pytest

from domains.learning.realtime import call_session as cs


class _WS:
    def __init__(self):
        self.out = []

    async def send_text(self, t):
        self.out.append(json.loads(t))

    async def send_bytes(self, b):
        self.out.append(("bytes", len(b)))


@pytest.fixture(autouse=True)
def _key_present(monkeypatch):
    monkeypatch.setattr(cs.os.path, "isfile", lambda p: True)   # 키가 있는 환경처럼(합성은 각 시험이 가짜로)


def _state():
    st = cs._CallState()
    st.tts_voice = "Leda"
    st.segments = [{"turn_index": 0, "role": "beaver", "text": "안녕, Tester! 요즘 무슨 재미있는 일 있었어?", "pcm": b""}]
    st.next_turn_index = 1
    return st


@pytest.mark.asyncio
async def test_mute_greeting_is_spoken_by_tts_with_subtitle(monkeypatch):
    monkeypatch.setattr(cs, "_greeting_tts_sync", lambda text, voice: b"\x01\x00" * 24000 * 2)   # 2초
    ws, st = _WS(), _state()
    ok = await cs._greeting_tts_fallback(ws, st, "안녕, Tester! 요즘 무슨 재미있는 일 있었어?")
    assert ok and st.greeting_tts_done
    kinds = [m["type"] if isinstance(m, dict) else m[0] for m in ws.out]
    assert kinds[0] == "turn_start" and kinds[1] == "output_transcript" and kinds[-1] == "turn_end" and "bytes" in kinds
    assert ws.out[1]["text"] == "안녕, Tester! 요즘 무슨 재미있는 일 있었어?"      # 자막 보류분 그대로
    assert sum(m[1] for m in ws.out if isinstance(m, tuple)) == 24000 * 2 * 2
    assert st.segments[-1]["pcm"] and st.segments[-1]["role"] == "beaver"           # 비버 발화로 소리와 함께 저장


@pytest.mark.asyncio
async def test_tts_failure_falls_back_to_reseed(monkeypatch):
    def boom(text, voice):
        raise RuntimeError("tts down")

    monkeypatch.setattr(cs, "_greeting_tts_sync", boom)
    ws, st = _WS(), _state()
    assert await cs._greeting_tts_fallback(ws, st, "안녕") is False
    assert ws.out == [] and not st.segments[-1]["pcm"]


@pytest.mark.asyncio
async def test_tts_fallback_only_once_per_call(monkeypatch):
    monkeypatch.setattr(cs, "_greeting_tts_sync", lambda text, voice: b"\x01\x00" * 2400)
    ws, st = _WS(), _state()
    assert await cs._greeting_tts_fallback(ws, st, "안녕")
    assert await cs._greeting_tts_fallback(ws, st, "안녕") is False


@pytest.mark.asyncio
async def test_no_text_or_voice_means_reseed():
    ws, st = _WS(), _state()
    assert await cs._greeting_tts_fallback(ws, st, "   ") is False
    st2 = _state(); st2.tts_voice = ""
    assert await cs._greeting_tts_fallback(ws, st2, "안녕") is False
