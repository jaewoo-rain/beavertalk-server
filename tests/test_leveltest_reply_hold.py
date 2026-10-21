"""2차 ③(2026-10-22) — 레벨테스트 종료 가능 턴에서 판정 동안 답 첫 소리 보류(2383: 짧은 답이 판정 중 다 흘러가 컷이 효과 없음)."""
import asyncio
import json

import pytest

from domains.learning.realtime import call_session as cs
from tests.test_leveltest_end_cut import _WS, _ev, _state


def _judge(end, delay=0.0):
    async def fake(*_a, **_k):
        if delay:
            await asyncio.sleep(delay)
        return (not end), end
    return fake


def _ready(st, *, answers=2, elapsed=60.0):
    st.total_answers = answers
    st.call_start_ts = asyncio.get_running_loop().time() - elapsed
    st.cur_user_text = ["몰라요"]
    st.last_beaver_question = "주말에 뭐 해요?"
    st.user_turn_open = True
    return st


def _types(ws):
    return [json.loads(j)["type"] if isinstance(j, str) else j.get("type") for j in ws.json]


@pytest.mark.asyncio
async def test_end_during_hold_cuts_whole_reply_turn(monkeypatch):
    monkeypatch.setattr(cs.svc, "judge_leveltest_turn", _judge(True))
    st, ws = _ready(_state()), _WS()
    await cs._leveltest_hold_reply(st)
    assert st.should_close and st.leveltest_cut_turn == cs._CUT_NEXT
    started = await cs._forward_event(ws, _ev("audio", audio=b"\x01\x02" * 100), st)
    assert started and st.leveltest_cut_turn == st.turn_id
    await cs._forward_event(ws, _ev("out_tr", text="벌써 모른다고? 그럼 다른 거 물어볼게."), st)
    await cs._forward_event(ws, _ev("audio", audio=b"\x03\x04" * 100), st)
    assert ws.bytes == [] and not st.cur_beaver_pcm and not st.cur_beaver_text
    assert cs._spawn_band_observe(st) is None and not st.leveltest_early_judged   # 턴 시작 때 같은 답변을 다시 띄우지 않음


@pytest.mark.asyncio
async def test_continue_during_hold_forwards_normally(monkeypatch):
    monkeypatch.setattr(cs.svc, "judge_leveltest_turn", _judge(False))
    st, ws = _ready(_state()), _WS()
    await cs._leveltest_hold_reply(st)
    assert not st.should_close and st.leveltest_cut_turn is None and st.leveltest_early_judged
    started = await cs._forward_event(ws, _ev("audio", audio=b"\x01\x02" * 100), st)
    assert started and ws.bytes
    assert cs._spawn_band_observe(st) is None and not st.leveltest_early_judged
    assert st.total_answers == 3


@pytest.mark.asyncio
async def test_slow_judge_times_out_then_922_cut_takes_current_turn(monkeypatch):
    monkeypatch.setattr(cs, "LEVELTEST_REPLY_HOLD_S", 0.05)
    monkeypatch.setattr(cs.svc, "judge_leveltest_turn", _judge(True, delay=0.3))
    st, ws = _ready(_state()), _WS()
    await cs._leveltest_hold_reply(st)
    assert not st.should_close and st.leveltest_cut_turn is None
    await cs._forward_event(ws, _ev("audio", audio=b"\x01\x02" * 100), st)
    reply = st.turn_id
    assert reply and ws.bytes
    await asyncio.gather(*list(st.band_tasks))
    assert st.should_close and st.leveltest_cut_turn == reply   # 남은 부분은 PM-DEC-922 컷
    n = len(ws.bytes)
    await cs._forward_event(ws, _ev("audio", audio=b"\x05\x06" * 100), st)
    assert len(ws.bytes) == n


@pytest.mark.asyncio
async def test_no_hold_before_end_is_possible(monkeypatch):
    called = []

    async def fake(*_a, **_k):
        called.append(1)
        return True, True
    monkeypatch.setattr(cs.svc, "judge_leveltest_turn", fake)
    st = _ready(_state(), answers=0, elapsed=60.0)      # 답변 1개째 — 최소 답변 3 전
    await cs._leveltest_hold_reply(st)
    st2 = _ready(_state(), answers=5, elapsed=20.0)     # 시간 플로어 45s 전
    await cs._leveltest_hold_reply(st2)
    assert called == [] and not st.leveltest_early_judged and not st2.leveltest_early_judged


@pytest.mark.asyncio
async def test_hold_off_switch(monkeypatch):
    monkeypatch.setattr(cs, "LEVELTEST_REPLY_HOLD_S", 0.0)
    monkeypatch.setattr(cs.svc, "judge_leveltest_turn", _judge(True))
    st = _ready(_state())
    await cs._leveltest_hold_reply(st)
    assert not st.leveltest_early_judged and not st.band_tasks


def test_end_possible_thresholds():
    st = _state()
    st.call_start_ts = None
    st.total_answers = cs.LEVELTEST_BAND_MAX_ANSWERS - 1
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(_async(cs._leveltest_end_possible, st)) is True   # 턴캡 직전
    finally:
        loop.close()


async def _async(fn, *a):
    return fn(*a)
