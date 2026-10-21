"""통화 중 소리 0B 비버 턴 → 받은 텍스트를 TTS 로 대체(call 2308 · command center 판단 A)."""
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
    monkeypatch.setattr(cs.os.path, "isfile", lambda p: True)


def _state():
    st = cs._CallState()
    st.tts_voice = "Fenrir"
    st.learner_spoke = True
    st.segments = [{"turn_index": 3, "role": "beaver", "text": "넌 친구 따라 병원 왔니?", "pcm": b""}]
    st.next_turn_index = 4
    return st


@pytest.mark.asyncio
async def test_silent_midcall_turn_is_spoken_with_clean_text(monkeypatch):
    seen = []
    monkeypatch.setattr(cs, "_greeting_tts_sync", lambda text, voice: seen.append(text) or b"\x01\x00" * 4800)
    ws, st = _WS(), _state()
    raw = ("너 **얼굴**이 안 좋아 보이는데?\n\nThe medical disclaimer: This is not medical advice or a diagnosis. "
           "Always see a healthcare professional or seek care.")
    assert await cs._midcall_tts_fallback(ws, st, raw)
    assert seen == ["너 얼굴이 안 좋아 보이는데?"]                      # 안내 문구·별표 걷은 뒤 합성
    kinds = [m["type"] if isinstance(m, dict) else m[0] for m in ws.out]
    assert kinds[0] == "turn_start" and kinds[1] == "output_transcript" and kinds[-1] == "turn_end" and "bytes" in kinds
    assert ws.out[1]["text"] == "너 얼굴이 안 좋아 보이는데?"           # 자막과 소리가 같은 문장
    assert st.segments[-1]["pcm"] and st.midcall_tts_count == 1


@pytest.mark.asyncio
async def test_failure_keeps_current_behaviour(monkeypatch):
    def boom(text, voice):
        raise RuntimeError("tts down")
    monkeypatch.setattr(cs, "_greeting_tts_sync", boom)
    ws, st = _WS(), _state()
    assert await cs._midcall_tts_fallback(ws, st, "넌 친구 따라 병원 왔니?") is False
    assert ws.out == [] and not st.segments[-1]["pcm"]


@pytest.mark.asyncio
async def test_interrupted_cap_empty_and_switch(monkeypatch):
    monkeypatch.setattr(cs, "_greeting_tts_sync", lambda text, voice: b"\x01\x00" * 2400)
    ws = _WS()
    st = _state(); st.midcall_tts_skip = True                     # 모델이 버린 턴
    assert await cs._midcall_tts_fallback(ws, st, "안녕") is False
    st = _state(); st.midcall_tts_count = cs.MIDCALL_TTS_MAX       # 통화당 상한
    assert await cs._midcall_tts_fallback(ws, st, "안녕") is False
    st = _state()
    assert await cs._midcall_tts_fallback(ws, st, "The medical disclaimer: This is not medical advice.") is False   # 걷고 나면 빈 문장
    monkeypatch.setattr(cs, "MIDCALL_TTS", False)
    assert await cs._midcall_tts_fallback(ws, _state(), "안녕") is False
    assert ws.out == []


def test_reconnect_brief_forbids_repeating_last_line():
    """call 2309 — 재연결 브리프가 인용한 마지막 말을 그대로 되풀이했다."""
    st = cs._CallState()
    st.learner_spoke = True
    st.segments = [{"turn_index": 17, "role": "beaver", "text": "어디가 어떻게 아픈지 말을 해봐!", "pcm": b""}]
    brief = cs._reconnect_brief(st)
    assert "«어디가 어떻게 아픈지 말을 해봐!»" in brief and "그 말을 다시 하지 마라" in brief


@pytest.mark.asyncio
async def test_masked_text_is_not_spoken(monkeypatch):
    """call 2309 t18 — 0B 턴 전사의 «◯◯◯» 가림 표시를 소리로 읽지 않는다."""
    monkeypatch.setattr(cs, "_greeting_tts_sync", lambda text, voice: b"\x01\x00" * 2400)
    ws, st = _WS(), _state()
    assert await cs._midcall_tts_fallback(ws, st, "너 진짜 어디 ◯◯◯ 거 아니야?") is False
    assert ws.out == []
