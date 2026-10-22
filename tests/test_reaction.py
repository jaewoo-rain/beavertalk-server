"""PM-DEC-1019 — 통화 반응(서버 발동) 결정적 시험. 판정기 · 클립 통화 대기 · 답 보류 연장 · 마이크 막음 · 끔 = 종전."""
from __future__ import annotations

import asyncio
import random
import time
from types import SimpleNamespace

import pytest

from domains.learning.realtime import call_session, clip_expr, reaction
from domains.learning.realtime.clip_expr import ClipExprSession
from test_clip_expr import SETTINGS, SILENT, VOICED

FAST = {k: (60, 30 if s else None, c) for k, (_ms, s, c) in reaction.SPEC.items()}


@pytest.fixture(autouse=True)
def _instant_playback(monkeypatch):
    monkeypatch.setattr(clip_expr, "_OUT_BPS", 10 ** 12)
    clip_expr._PROGRESS.clear()
    reaction._GATES.clear()                                        # 판정기는 call_id 로 잇는다 — 시험마다 비운다


@pytest.fixture
def rx_on(monkeypatch):
    monkeypatch.setattr(reaction, "REACTION_ON", True)
    monkeypatch.setattr(reaction, "SPEC", FAST)
    monkeypatch.setattr(reaction, "MIN_GAP_S", 0.0)


# ── 판정기 ────────────────────────────────────────────────────────────────── #
def test_gate_correct_streak_gets_short_version():
    g = reaction.ReactionGate()
    a = g.decide("correct", 0.0)
    b = g.decide("correct", 100.0)
    assert a["duration_ms"] == 4000 and b["duration_ms"] == 2000
    g.decide("wrong", 200.0)
    assert g.decide("correct", 300.0)["duration_ms"] == 4000      # 오답이 끼면 다시 긴 판


def test_gate_wrong_cap_and_consecutive_wrong_not_laughed_at():
    g = reaction.ReactionGate()
    assert g.decide("wrong", 0.0) is not None
    assert g.decide("wrong", 100.0) is None                       # 연속 오답 2번째 — 웃지 않는다
    g.decide("correct", 200.0)
    assert g.decide("wrong", 300.0) is not None
    g.decide("correct", 400.0)
    assert g.decide("wrong", 500.0) is not None
    g.decide("correct", 600.0)
    assert g.decide("wrong", 700.0) is None                        # 통화당 3회
    assert g.counts["wrong"] == 3


def test_gate_no_overlap_gap_and_total_cap(monkeypatch):
    g = reaction.ReactionGate()
    assert g.decide("pron", 0.0)["duration_ms"] == 3000
    assert g.active(1.0) and g.decide("correct", 1.0) is None       # 반응 중 — 겹치지 않는다
    assert g.decide("pron", 3.5) is None                           # 끝난 뒤 최소 간격 전
    assert g.decide("pron", 3.0 + reaction.MIN_GAP_S + 0.1) is not None
    assert g.decide("laugh", 100.0) is None                        # 자유대화 웃음 반응은 없다(PM-DEC-1021)
    monkeypatch.setattr(reaction, "TOTAL_CAP_S", 1.0)
    assert reaction.ReactionGate().decide("correct", 0.0) is None   # 총 시간 상한


def test_off_means_no_gate():
    state = SimpleNamespace(rx_gate=None)
    assert reaction.REACTION_ON is False
    assert reaction.gate(state) is None and state.rx_gate is None


# ── 클립 통화: 판정 → 반응 → (그 길이만큼 기다림) → 비버 클립 ─────────────────── #
class _H:
    def __init__(self, results):
        self.results = list(results)
        self.log: list[tuple[float, dict]] = []
        self.state = SimpleNamespace(call_id=7, close_seed="CLOSE", call_start_ts=None, fragment_index=1, rx_gate=None,
                                     rx_send=self._send)

        async def judge(pcm, ctx):
            r = self.results.pop(0)
            return "말", r

        async def notify(msg):
            self.log.append((time.monotonic(), msg))

        self.s = ClipExprSession(None, None, SETTINGS, {}, char="Baba", state=self.state, notify=notify, judge=judge,
                                 request_close=lambda: None, rng=random.Random(0))

    async def _send(self, msg):
        self.log.append((time.monotonic(), msg))

    async def turn(self):
        while True:
            ev = await asyncio.wait_for(self.it.__anext__(), 2)
            if ev.kind == "turn_end":
                return

    async def say(self):
        for _ in range(8):
            await self.s.send_audio(VOICED)
        for _ in range(4):
            await self.s.send_audio(SILENT)


@pytest.mark.asyncio
async def test_clip_correct_sends_reaction_then_waits_before_clips(rx_on):
    h = _H(["correct"])
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        n = len(h.log)
        await h.say()
        await h.turn()
    after = h.log[n:]
    assert after[0][1] == {"type": "reaction", "kind": "correct", "duration_ms": 60, "id": 1}
    first_clip = next(t for t, m in after if m.get("type") == "clip")
    assert first_clip - after[0][0] >= 0.05                       # 반응 동안 비버 클립이 안 나갔다


@pytest.mark.asyncio
async def test_clip_wrong_reaction_drops_laugh_clip(rx_on):
    h = _H(["wrong"])
    h.s._rng = random.Random(0)
    h.s._rng.random = lambda: 0.0                                 # 웃음 클립 확률 100% — 반응이 나가면 그래도 안 붙는다
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        n = len(h.log)
        await h.say()
        await h.turn()
    msgs = [m for _, m in h.log[n:]]
    assert msgs[0]["type"] == "reaction" and msgs[0]["kind"] == "wrong"
    assert not any(m.get("id", "").startswith("g_laugh") for m in msgs if m.get("type") == "clip")


@pytest.mark.asyncio
async def test_clip_off_sends_nothing_and_keeps_laugh():
    h = _H(["wrong"])
    h.s._rng.random = lambda: 0.0
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        n = len(h.log)
        await h.say()
        await h.turn()
    msgs = [m for _, m in h.log[n:]]
    assert not any(m["type"] == "reaction" for m in msgs)
    assert msgs[0]["id"].startswith("g_laugh")                     # 끔 = 종전 그대로


# ── Live 통화: 답 보류 연장 · 마이크 막음 ───────────────────────────────────── #
@pytest.mark.asyncio
async def test_live_hold_kept_and_mic_muted_while_reacting(rx_on):
    state = SimpleNamespace(rx_gate=reaction.ReactionGate(), hold_events=[SimpleNamespace(kind="audio")],
                            reply_mute_until=0.0)
    now = asyncio.get_running_loop().time()
    state.rx_gate.until = now + 5.0
    assert call_session._reply_hold_poll(state) == []
    assert state.hold_events is not None                           # 붙잡은 답을 버리지도 풀지도 않았다
    assert call_session._reply_muted(state) is True
    state.rx_gate.until = now - 1.0
    assert call_session._reply_muted(state) is False


@pytest.mark.asyncio
async def test_fire_returns_zero_when_send_fails(rx_on):
    async def boom(msg):
        raise RuntimeError("ws closed")
    state = SimpleNamespace(rx_gate=None, call_id=1)
    assert await reaction.fire(state, "correct", boom, where="t") == 0
    assert not reaction.active(state)                              # 못 보냈으면 반응 중으로 치지 않는다


# ── 대본 읽기: 첫 시도 고유사도 → 발음 감탄(통화당 2회) · 완주 → 축하(종료 턴보다 먼저) ─────────── #
@pytest.mark.asyncio
async def test_script_pron_capped_and_finish_reaction_before_close(rx_on):
    import test_script_read as tsr
    from domains.learning.realtime import script_read as sr
    lines = sr.load_bank("Baba")[2]
    h = tsr._H([lines[i]["learner"] for i in range(1, 12)])
    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)
        h.notes.append(msg)

    h.s._state.rx_gate = None
    h.s._state.rx_send = send
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        for i in range(11):
            await h.say(turn=i < 10)
        await asyncio.sleep(0.15)                       # 완주 반응(60ms) 기다림
        assert [m["kind"] for m in sent] == ["pron", "pron"] + ["correct"] * 8 + ["script"]
        assert [m["duration_ms"] for m in sent][2:4] == [60, 30]    # 발음 상한 뒤 정답 반응 · 연속이면 짧은 판
        end_at = next(i for i, m in enumerate(h.notes) if m["type"] == "script_end")
        assert h.notes[-1]["type"] == "reaction" and h.notes.index(sent[-1]) > end_at
        await h.s.send_text_turn("CLOSE")
        await h.turn()


def test_gate_carries_over_fragments_of_same_call(rx_on):
    reaction._GATES.clear()
    a = SimpleNamespace(rx_gate=None, call_id=99)
    g = reaction.gate(a)
    g.decide("wrong", 0.0)
    b = SimpleNamespace(rx_gate=None, call_id=99)            # 이어하기 조각 = 새 상태 객체 · 같은 call_id
    assert reaction.gate(b) is g and g.counts["wrong"] == 1
    c = SimpleNamespace(rx_gate=None, call_id=100)
    assert reaction.gate(c) is not g


@pytest.mark.asyncio
async def test_judge_timing_note_and_log(caplog):
    st = SimpleNamespace(jt_in_tr_ts=None, jt_reply_ts=None, turn_id=None, hold_events=None, call_id=5)
    call_session._jt_note(st, SimpleNamespace(kind="in_tr", text="안녕"))
    call_session._jt_note(st, SimpleNamespace(kind="audio", text=None))
    assert st.jt_in_tr_ts is not None and st.jt_reply_ts is not None
    st.hold_events = []
    t = st.jt_reply_ts
    call_session._jt_note(st, SimpleNamespace(kind="audio", text=None))
    assert st.jt_reply_ts == t                                     # 붙잡힌 답의 뒤 조각은 도착 시각을 덮지 않는다
    with caplog.at_level("INFO"):
        call_session._jt_log(st, 1, False, (t - 2.0, t - 0.5, t), 1200, True)
    assert ("필요대기_ms=700" in caplog.text or "필요대기_ms=699" in caplog.text) and "말끝→답도착_ms=2000" in caplog.text


@pytest.mark.asyncio
async def test_script_first_fail_reacts_wrong_then_same_line(rx_on):
    import test_script_read as tsr
    from domains.learning.realtime import script_read as sr
    lines = sr.load_bank("Baba")[2]
    h = tsr._H(["완전히 다른 말입니다 정말로", lines[1]["learner"]])
    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)
        h.notes.append(msg)

    h.s._state.rx_gate = None
    h.s._state.rx_send = send
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        await h.say(turn=False)
        await asyncio.sleep(0.1)
        assert [m["kind"] for m in sent] == ["wrong"]
        i = h.notes.index(sent[0])
        assert [m["type"] for m in h.notes[i + 1:i + 3]] == ["script_line", "script_turn"]   # 반응 뒤 같은 줄 다시
