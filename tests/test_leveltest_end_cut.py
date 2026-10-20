"""PM-DEC-922 — 레벨테스트 종료 판정이 다음 질문 생성 중에 오면 남은 질문을 앱에 보내지 않는다(call 2234)."""
import asyncio

import pytest

from domains.learning.realtime import call_session as cs


class _WS:
    def __init__(self):
        self.bytes = []
        self.json = []

    async def send_bytes(self, b):
        self.bytes.append(b)

    async def send_text(self, t):
        self.json.append(t)

    async def send_json(self, j):
        self.json.append(j)


def _ev(kind, **kw):
    base = dict(kind=kind, audio=None, text=None, is_final=None, fn_name=None, fn_id=None, fn_args=None)
    base.update(kw)
    return type("E", (), base)()


def _judge(end):
    async def fake(*_a, **_k):
        return True, end
    return fake


def _state():
    st = cs._CallState()
    st.band_observe = True
    st.is_leveltest = True
    st.call_start_ts = 0.0
    return st


@pytest.mark.asyncio
async def test_end_judge_mid_question_marks_turn_cut(monkeypatch):
    monkeypatch.setattr(cs, "LEVELTEST_BAND_TIME_FLOOR_S", 0.0)
    monkeypatch.setattr(cs, "LEVELTEST_END_JUDGE_MIN_ANSWERS", 1)
    monkeypatch.setattr(cs.svc, "judge_leveltest_turn", _judge(True))
    st = _state()
    st.call_start_ts = asyncio.get_running_loop().time() - 100.0
    st.turn_id = "q1"
    await cs._band_observe_sidecar(st, "너무 길어요", "주말에 뭐 해요?", reply_turn_id="q1")
    assert st.should_close and st.leveltest_cut_turn == "q1"


@pytest.mark.asyncio
async def test_no_cut_when_question_turn_already_ended(monkeypatch):
    monkeypatch.setattr(cs, "LEVELTEST_BAND_TIME_FLOOR_S", 0.0)
    monkeypatch.setattr(cs, "LEVELTEST_END_JUDGE_MIN_ANSWERS", 1)
    monkeypatch.setattr(cs.svc, "judge_leveltest_turn", _judge(True))
    st = _state()
    st.call_start_ts = asyncio.get_running_loop().time() - 100.0
    st.turn_id = None                      # 질문이 이미 끝났다 → 워처가 바로 작별(기존 경로)
    await cs._band_observe_sidecar(st, "너무 길어요", "주말에 뭐 해요?", reply_turn_id="q1")
    assert st.should_close and st.leveltest_cut_turn is None


@pytest.mark.asyncio
async def test_no_cut_when_not_ending(monkeypatch):
    monkeypatch.setattr(cs.svc, "judge_leveltest_turn", _judge(False))
    st = _state()
    st.call_start_ts = asyncio.get_running_loop().time() - 100.0
    st.turn_id = "q1"
    await cs._band_observe_sidecar(st, "저는 서울에 살아요", "어디 살아요?", reply_turn_id="q1")
    assert not st.should_close and st.leveltest_cut_turn is None


@pytest.mark.asyncio
async def test_cut_turn_audio_and_text_are_not_forwarded():
    st = _state()
    ws = _WS()
    st.turn_id = "q1"
    st.leveltest_cut_turn = "q1"
    await cs._forward_event(ws, _ev("audio", audio=b"\x01\x02" * 100), st)
    await cs._forward_event(ws, _ev("out_tr", text="한국에서 제일 가보고 싶은 데가"), st)
    assert ws.bytes == [] and not st.cur_beaver_pcm and not st.cur_beaver_text
    # 다음 턴(작별)은 그대로 나간다
    st.turn_id = None
    started = await cs._forward_event(ws, _ev("audio", audio=b"\x03\x04" * 100), st)
    assert started and ws.bytes
