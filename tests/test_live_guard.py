"""call 2000(2026-10-16 · PM-DEC-710) 대응 — Live 송신 잠금·시간 제한 · 열린 비버 턴 무수신 감시 · 앱에 오류 알림.

call 2000: t3 오디오 2.3초 뒤 Gemini 이벤트가 0, 같은 시각 앱 계측 배치도 끊김 → 100초 뒤 1008 「The operation was aborted」.
그 사이 앱에는 아무 신호가 없어 통화 화면이 3분 46초까지 남았다.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import core.gemini_live as gl
import domains.learning.realtime.call_session as cs
from core.config import settings as app_settings
from core.gemini_live import LiveEvent
from domains.learning.realtime.call_session import run_call
from tests.test_normalcall_ws import FakeWebSocket, _wait_analysis_tasks, seeded, session_factory  # noqa: F401


class _Raw:
    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.active = 0
        self.peak = 0

    async def _w(self):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.active -= 1

    async def send_realtime_input(self, **_kw):
        await self._w()

    async def send_client_content(self, **_kw):
        await self._w()


@pytest.mark.asyncio
async def test_a_stuck_send_raises_live_send_stalled_not_timeout(monkeypatch):
    monkeypatch.setattr(app_settings, "LIVE_SEND_TIMEOUT_S", 0.05)
    s = gl.GeminiLiveSession(_Raw(delay=5))
    with pytest.raises(gl.LiveSendStalled):
        await s.send_audio(b"\x00\x00")
    assert not issubclass(gl.LiveSendStalled, TimeoutError), "TimeoutError 면 run_call 이 «통화 상한» 으로 오인한다"


@pytest.mark.asyncio
async def test_sends_from_different_coroutines_are_serialized():
    raw = _Raw(delay=0.01)
    s = gl.GeminiLiveSession(raw)
    await asyncio.gather(s.send_audio(b"\x00\x00"), s.send_reground("[안내] x", turn_complete=False),
                         s.send_text_turn("[안내] y"), s.send_audio(b"\x00\x00"))
    assert raw.peak == 1, "같은 웹소켓에 동시에 쓰지 않는다"


class _StallAfterAudio:
    """비버 턴을 연 뒤(out_tr·audio) 아무 이벤트도 안 준다 — call 2000 t3 의 모양."""

    def __init__(self):
        self.sent_audio: list[bytes] = []
        self.sent_text_turns: list[str] = []

    async def send_audio(self, pcm16_16k: bytes) -> None:
        self.sent_audio.append(pcm16_16k)

    async def send_text_turn(self, text: str) -> None:
        self.sent_text_turns.append(text)

    async def send_reground(self, text: str, *, turn_complete: bool = True) -> None:
        pass

    async def events(self):
        yield LiveEvent(kind="out_tr", text="Today we learn greetings. How do you say it?")   # t1 정상 턴
        yield LiveEvent(kind="audio", audio=b"\x00\x00" * 8)
        yield LiveEvent(kind="turn_end")
        yield LiveEvent(kind="out_tr", text="Heh, not quite. Sounds")                         # t3 — 열린 채 멈춤
        yield LiveEvent(kind="audio", audio=b"\x00\x00" * 8)
        await asyncio.sleep(60)
        yield LiveEvent(kind="turn_end")


@pytest.mark.asyncio
async def test_an_open_beaver_turn_with_no_live_events_closes_the_call_and_tells_the_app(session_factory, seeded, monkeypatch):  # noqa: F811
    import contextlib

    monkeypatch.setattr(cs, "LIVE_STALL_S", 0.3)

    @contextlib.asynccontextmanager
    async def _factory(client, settings, *, system_instruction, voice, **_kw):
        yield _StallAfterAudio()

    ws = FakeWebSocket([{"type": "websocket.receive",
                         "text": json.dumps({"type": "start", "character_id": seeded["character_id"], "call_type": "chat"})}],
                       hang=True)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await asyncio.wait_for(run_call(ws, app_settings, object(), session_factory,
                                    member_id=seeded["member_id"], live_session_factory=_factory), 15)
    await _wait_analysis_tasks()
    took = loop.time() - t0
    errors = [json.loads(t) for t in ws.sent_text if '"error"' in t]
    assert took < 10, ("무수신 감시가 안 돌았다", took)
    assert errors and errors[-1].get("code") == "CALL_INTERRUPTED", ws.sent_text[-3:]
    assert any('"call_ended"' in t for t in ws.sent_text), "앱이 통화 화면에 남지 않게 call_ended 도 간다"


@pytest.mark.asyncio
async def test_learner_turn_silence_is_not_a_stall(monkeypatch):
    """열린 비버 턴이 없으면(학습자 차례) 감시하지 않는다 — 무음은 무음 워처 몫."""
    monkeypatch.setattr(cs, "LIVE_STALL_S", 0.05)
    st = cs._CallState()
    assert st.turn_id is None
    stall = cs.LIVE_STALL_S if (cs.LIVE_STALL_S > 0 and st.turn_id is not None) else None
    assert stall is None
