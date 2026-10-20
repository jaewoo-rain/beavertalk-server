"""PM-DEC-891 — 학습자 목소리 프레임이 들어오는 동안은 무음 3단이 작별하지 않는다(call 2070·2071)."""
import asyncio

import pytest

from domains.learning.realtime import call_session as cs


class _Sess:
    def __init__(self):
        self.sent = []

    async def send_text_turn(self, text):
        self.sent.append(text)


def _state(loop):
    st = cs._CallState()
    st.call_start_ts = loop.time()
    st.silence_stage = 2
    st.last_activity_ts = loop.time() - 100.0
    st.idle_nudge1_s, st.idle_nudge2_s, st.idle_close_s = 60.0, 10.0, 0.3
    return st


@pytest.mark.asyncio
async def test_voice_frames_hold_off_close(monkeypatch):
    closed = []

    async def _close(session, state):
        closed.append(1)

    monkeypatch.setattr(cs, "_inject_close_seed", _close)
    loop = asyncio.get_running_loop()
    st = _state(loop)

    async def _talk():
        for _ in range(8):
            st.user_voice_ts = loop.time()
            await asyncio.sleep(0.1)

    task = asyncio.create_task(cs._watch_idle(_Sess(), st))
    await _talk()
    assert not closed and not st.should_close   # 말하는 동안(0.8s > close 0.3s)은 안 끊는다
    await asyncio.wait_for(task, 3.0)            # 말이 멎으면 그때부터 0.3s 뒤 작별
    assert closed and st.should_close


@pytest.mark.asyncio
async def test_without_voice_closes_as_before(monkeypatch):
    closed = []

    async def _close(session, state):
        closed.append(1)

    monkeypatch.setattr(cs, "_inject_close_seed", _close)
    st = _state(asyncio.get_running_loop())
    await asyncio.wait_for(cs._watch_idle(_Sess(), st), 2.0)
    assert closed
