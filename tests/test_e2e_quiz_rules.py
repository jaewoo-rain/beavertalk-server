"""E2E 하네스 — 3차 묶음 퀴즈 규칙 기대(bt-back 2026-09-15): ① 미출제 3개가 모이면 큐 ② 블록 안 항목 번호 오름차순
③(되돌림 — 판정 창은 여는 비버 턴 포함, 그 턴 공개면 failed) ④ 전사 없는(무음) 답 턴은 학습자 턴이 아니다. 서버·DB 없이."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import e2e_expression_call as h  # noqa: E402


class _Voice:
    async def pcm(self, text, lang):
        return b"\0" * 320


class _Picker:
    enabled = False
    calls = 0
    client = None


def _item(i, surface, en):
    return h.Item(i, surface, en, "", (en,), kind="vocab")


def _session():
    items = {i: _item(i, s, e) for i, s, e in [(1, "사람", "person"), (2, "나라", "country"), (3, "고향", "hometown"), (4, "이름", "name")]}
    return h.Session(items, _Voice(), _Picker(), probe=True, verbose=False, course="expression", lesson={"no": 4})


def _rec(sess, iid, k):
    r = h.ItemRecord(item=sess.items[iid], k=k, policy=1)
    sess.records[iid] = r
    sess.drilled_order.append(iid)
    return r


# ── ① 미출제 3개 ─────────────────────────────────────────────────────── #
def test_unquizzed_counts_only_drilled_items_without_rounds():
    sess = _session()
    for k, iid in enumerate([1, 2, 3, 4], 1):
        _rec(sess, iid, k)
    sess.records[1].rounds.append(h.QuizRound(n=1, asked_at=1.0, block=1))     # 이미 퀴즈에 오름(틀렸어도 출제됨)
    sess.records[4].superseded_by = 2                                         # 오식별 대체 항목은 세지 않는다
    assert sess.unquizzed_ids() == [2, 3]


def test_anchor_ok_when_three_unquizzed_even_if_drilled_total_not_multiple_of_three():
    sess = _session()
    for k, iid in enumerate([1, 2, 3, 4], 1):
        _rec(sess, iid, k)
    sess.records[1].rounds.append(h.QuizRound(n=1, asked_at=1.0, block=1))    # 드릴 4 · 미출제 3 → 옛 규칙(4%3)은 ✖, 새 규칙은 ✔
    asyncio.run(sess.on_beaver_turn("Okay, quiz time! How do you say \"country\"?", None))
    assert len(sess.anchors) == 1 and sess.anchor_pending == [[2, 3, 4]]
    assert any("미출제3✔" in t for t in sess.turns[-1].tags)


# ── ② 번호 오름차순 ─────────────────────────────────────────────────── #
def test_quiz_order_check_ascending_by_item_number_per_block():
    blocks = {1: [10, 11, 12], 2: [14, 13]}
    num_of = {10: 1, 11: 2, 12: 3, 13: 4, 14: 5}
    ok, lines = h.quiz_order_check(blocks, num_of)
    assert ok is False and "1 → 2 → 3 ✔" in lines[0] and "5 → 4 ✖" in lines[1]
    assert h.quiz_order_check({1: [10, 99, 12]}, num_of)[0] is True      # 목록 밖(?)은 빼고 본다
    assert h.quiz_order_check({1: [12, 10]}, {})[0] is True               # 번호 없음 = 판단 불가


def test_quiz_blocks_first_ask_order_and_skips_unanchored():
    sess = _session()
    for k, iid in enumerate([1, 2, 3], 1):
        _rec(sess, iid, k)
    sess.records[2].rounds.append(h.QuizRound(n=1, asked_at=5.0, block=1))
    sess.records[1].rounds.append(h.QuizRound(n=1, asked_at=6.0, block=1))
    sess.records[2].rounds.append(h.QuizRound(n=2, asked_at=7.0, block=1))    # 같은 블록 재질문 — 첫 번만
    sess.records[3].rounds.append(h.QuizRound(n=1, asked_at=8.0, anchored=False))
    assert h.quiz_blocks(sess.records) == {1: [2, 1]}


# ── ③④ 기대 passed ──────────────────────────────────────────────────── #
def test_expected_passed_window_includes_opening_turn_and_requires_heard_answer():
    sess = _session()
    r = _rec(sess, 1, 1)
    # 여는 턴에서 비버가 정답을 공개 → 회차 revealed · 복창 → 기대 failed
    r.rounds.append(h.QuizRound(n=1, asked_at=1.0, block=1, revealed=True, answers=["parrot"], heard=True))
    assert r.expected_passed is False and r.unjudgeable_correct == ""
    # 여는 턴(앵커) 회차의 자발 정답이라도 전사가 안 왔으면 무음 턴
    rd2 = h.QuizRound(n=2, asked_at=2.0, block=2, spontaneous_correct=True, heard=False)
    r.rounds.append(rd2)
    assert r.expected_passed is False and r.unjudgeable_correct == "무음 턴(전사 없음)"
    rd2.heard = True
    assert r.expected_passed is True and r.unjudgeable_correct == "" and r.expectation_ambiguous is False
    # 앵커 없는 재출제의 들린 정답만 있으면 passed~(보류 허용)
    r2 = _rec(sess, 2, 2)
    r2.rounds.append(h.QuizRound(n=1, asked_at=3.0, anchored=False, spontaneous_correct=True, heard=True))
    assert r2.expected_passed is True and r2.expectation_ambiguous is True


def test_transcript_arrival_marks_linked_quiz_round_heard_only():
    sess = _session()
    r = _rec(sess, 1, 1)
    sess.mode = "quiz"
    sess.current = r
    r.rounds.append(h.QuizRound(n=1, asked_at=1.0, block=1, spontaneous_correct=True))
    t = sess.add_turn("learner", "사람요", kind="correct", item_id=1)
    sess._link_answer_turn(t)
    sess.mode = "drill"
    t_drill = sess.add_turn("learner", "사람요", kind="parrot", item_id=1)
    sess._link_answer_turn(t_drill)                    # 드릴 모드(앵커 회차) 답은 달지 않는다
    assert r.rounds[0].answer_turns == [t.n]
    asyncio.run(sess.on_json({"type": "input_transcript", "text": "사람요"}, None))   # 가장 최근 전사 없는 학습자 턴(t_drill)에 붙는다
    assert r.rounds[0].heard is False
    asyncio.run(sess.on_json({"type": "input_transcript", "text": "사람요"}, None))   # 그 앞 턴(t) → 회차 heard
    assert r.rounds[0].heard is True and r.expected_passed is True
