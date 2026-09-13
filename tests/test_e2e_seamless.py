"""E2E 하네스 --seamless(H8) — ① 프로토콜 프레임(하네스가 보내는 start.silent_resume/fragment_end 가 서버 어댑터를 통과하고,
서버 fragment_saved/call_started 가 하네스가 읽는 키로 나온다) ② 하네스 상태기계(전환 대기 → «학습자 발화 → 비버 turn_end» 에서만
fragment_end · fragment_saved 기록 · 조각2 무음 관찰 중 비버 출력 계수·보류) ③ (f) turn_index 검사. 서버·DB·소켓 없이."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import e2e_expression_call as h  # noqa: E402
from domains.learning.realtime import protocol as P  # noqa: E402


class _Voice:
    async def pcm(self, text, lang):
        return b"\0" * 320


class _Picker:
    enabled = False
    calls = 0
    client = None


class _Uplink:
    open = False

    def cut(self):
        pass

    async def speak(self, pcm):
        await asyncio.sleep(0)


def _items():
    return {24: h.Item(24, "이", "this", "", ("this",), kind="vocab", example="이 사람은 제 동생이에요."),
            73: h.Item(73, "말레이시아", "Malaysia", "", ("malaysia",), kind="vocab", example="말레이시아는 나라예요.")}


def _session(**kw):
    s = h.Session(_items(), _Voice(), _Picker(), probe=False, verbose=False, course="expression", lesson={"no": 4})
    h.PRE_SPEECH_S = 0.0
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ── ① 프로토콜 ──────────────────────────────────────────────────────────── #
def test_protocol_frames_round_trip_with_harness_shapes():
    start = P.client_adapter.validate_python({"type": "start", "character_id": 1, "locale": "en", "duration_min": 5, "call_type": "expression",
                                              "continues_call_id": "1592", "silent_resume": True})
    assert start.silent_resume is True and str(start.continues_call_id) == "1592"
    fe = P.client_adapter.validate_python({"type": "fragment_end"})
    assert isinstance(fe, P.ClientFragmentEnd)
    saved = json.loads(P.ServerFragmentSaved(call_id="1592", fragment_index=1).model_dump_json())
    assert saved == {"type": "fragment_saved", "call_id": "1592", "fragment_index": 1}
    started = json.loads(P.ServerCallStarted(call_id="1592", character_id=1, course="expression", fragment_index=2, max_fragments=3).model_dump_json())
    assert started["fragment_index"] == 2 and started["max_fragments"] == 3
    old = json.loads(P.ServerCallStarted(call_id="1", character_id=1).model_dump_json())
    assert "fragment_index" not in old and "max_fragments" not in old      # 옛 경로 프레임 바이트 동일


# ── ② 상태기계 ─────────────────────────────────────────────────────────── #
async def _run_switch(sess, up, sent):
    async def send_ctrl(d):
        sent.append((sess.now(), d))
    sess.send_ctrl = send_ctrl
    await sess.on_json({"type": "call_started", "call_id": "1592", "course": "expression", "fragment_index": 1, "max_fragments": 3}, up)
    # 전환 대기 전 비버 턴 → 학습자가 답한다(발화 시각 < switch_at)
    await sess.on_json({"type": "turn_start", "turn_id": "a"}, up)
    await sess.on_json({"type": "output_transcript", "text": 'How do you say "Malaysia"?'}, up)
    await sess.on_json({"type": "turn_end", "turn_id": "a"}, up)
    if sess.pending_speak:
        await asyncio.wait_for(asyncio.shield(sess.pending_speak), 5)
    assert sess.last_learner is not None and not sent
    sess.switch_at = sess.now() + 0.01          # 지금부터 전환 대기(직전 학습자 발화는 그 전이다)
    await asyncio.sleep(0.02)
    # 비버 응답 turn_end — 학습자 발화가 switch_at 보다 앞이므로 아직 fragment_end 가 아니다 → 정상 답변
    await sess.on_json({"type": "turn_start", "turn_id": "b"}, up)
    await sess.on_json({"type": "output_transcript", "text": 'Right! Now "this"?'}, up)
    await sess.on_json({"type": "turn_end", "turn_id": "b"}, up)
    if sess.pending_speak:
        await asyncio.wait_for(asyncio.shield(sess.pending_speak), 5)
    assert not sent and sess.last_learner.t >= sess.switch_at
    # 그 답에 대한 비버 turn_end → 여기서 fragment_end(답하지 않는다)
    n_learner = sum(1 for t in sess.turns if t.role == "learner")
    await sess.on_json({"type": "turn_start", "turn_id": "c"}, up)
    await sess.on_json({"type": "output_transcript", "text": "Good. 이."}, up)
    await sess.on_json({"type": "turn_end", "turn_id": "c"}, up)
    assert [d for _, d in sent] == [{"type": "fragment_end"}]
    assert sess.switching and sess.fragment_end_sent_at is not None and sess.hold_reply
    assert sum(1 for t in sess.turns if t.role == "learner") == n_learner          # 답하지 않았다
    assert "조각 경계(이 턴 뒤 fragment_end)" in sess.turns[-1].tags
    await sess.on_json({"type": "fragment_saved", "call_id": "1592", "fragment_index": 1}, up)
    assert sess.fragment_saved_at is not None and sess.fragment_saved["fragment_index"] == 1 and not sess.call_ended_before_saved
    assert sess.end_reason == "fragment_saved"


def test_fragment_end_only_after_learner_speech_then_beaver_turn_end():
    sess = _session(seamless=True)
    sent: list = []
    asyncio.run(_run_switch(sess, _Uplink(), sent))


def test_call_ended_before_saved_is_flagged():
    sess = _session(seamless=True, switching=True, fragment_end_sent_at=1.0)
    asyncio.run(sess.on_json({"type": "call_ended", "call_id": "1592", "reason": "done"}, _Uplink()))
    assert sess.call_ended_before_saved is True and sess.ended


async def _run_silent(sess, up):
    await sess.on_json({"type": "call_started", "call_id": "1592", "course": "expression", "fragment_index": 2, "max_fragments": 3}, up)
    assert sess.hold_reply and up.open and sess.fragment_index == 2 and sess.max_fragments == 3
    # 관찰 중 비버가 먼저 말한다(위반) — 계수만 하고 답하지 않는다
    await sess.on_json({"type": "turn_start", "turn_id": "x"}, up)
    await sess.on_json({"type": "output_transcript", "text": "Hey, are you there?"}, up)
    await sess.on_json({"type": "turn_end", "turn_id": "x"}, up)
    assert sess.pre_speech["turn_starts"] == 1 and sess.pre_speech["transcripts"] == ["Hey, are you there?"]
    assert not [t for t in sess.turns if t.role == "learner"] and "보류(전환/무음 관찰)" in sess.turns[-1].tags
    assert sess.beaver_turn_times and sess.beaver_turn_times[0][1] == "Hey, are you there?"
    # 학습자가 말한 뒤의 출력은 세지 않는다
    sess.first_speech_at = sess.now()
    await sess.on_json({"type": "turn_start", "turn_id": "y"}, up)
    await sess.on_json({"type": "output_transcript", "text": "Okay."}, up)
    assert sess.pre_speech["turn_starts"] == 1 and len(sess.pre_speech["transcripts"]) == 1


def test_silent_resume_counts_beaver_output_before_first_speech_and_holds_reply():
    sess = _session(silent_resume=True)
    asyncio.run(_run_silent(sess, _Uplink()))


# ── ③ (f) turn_index ───────────────────────────────────────────────────── #
def test_raw_turn_index_check():
    ok, d = h.raw_turn_index_check([(0, "beaver"), (1, "user"), (2, "beaver"), (3, "user")])
    assert ok and "중복 0" in d
    ok, d = h.raw_turn_index_check([(0, "beaver"), (1, "user"), (1, "beaver"), (3, "user")])     # 중복 + 건너뜀(재연결이 앞지른 꼴)
    assert not ok and "중복 1" in d and "건너뜀 1" in d
    assert h.raw_turn_index_check([])[0] is False


def test_seamless_checks_table_shapes():
    s1 = _session(seamless=True, fragment_end_sent_at=100.0, fragment_saved_at=100.9, ws_closed_at=101.0, ws_close_code=1000,
                  fragment_saved={"fragment_index": 1}, end_reason="fragment_saved")
    s2 = _session(silent_resume=True, resumed=True, fragment_index=2, max_fragments=3, started_at=0.5, first_speech_at=16.0)
    s2.pre_speech = {"audio_bytes": 0, "transcripts": [], "turn_starts": 0, "watch_s": 15}
    s2.add_turn("beaver", "Right, 이. Next?")
    sc1 = h.Score(); sc1.cur = {"cur_call": {"recorded_fragment": 1}}
    sc2 = h.Score(); sc2.cur = {"cur_call": {"recorded_fragment": 2}}; sc2.call_row = {"fragment_count": 2}
    rows = h.seamless_checks(s1, s2, plan_frag=3, sc1=sc1, sc2=sc2, raw_rows=[(0, "beaver"), (1, "user")])
    by = {r[0][:3]: r for r in rows}
    assert [r[3] for r in rows] == [True] * 6, rows
    assert "900ms" in by["(a)"][2]
    s2.pre_speech["audio_bytes"] = 4800
    assert h.seamless_checks(s1, s2, plan_frag=3, sc1=sc1, sc2=sc2, raw_rows=[(0, "beaver")])[2][3] is False
