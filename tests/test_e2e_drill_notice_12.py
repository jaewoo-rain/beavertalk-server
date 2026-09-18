# -*- coding: utf-8 -*-
"""12차 계기 — 드릴 안내(항목별 1회·통화당 6회) · 안내 주입 tc(완결 턴은 2.5 만) · call_id 전 줄.

근거 줄은 11차 재검 실측(1645 2.5 · 1646 3.1)에서 그대로 가져왔다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import e2e_expression_call as h  # noqa: E402

P25 = "2026-09-18T09:24:29.000000Z\tINFO:domains.learning.realtime.call_session:normalcall "
M25 = "gemini-live-2.5-flash-native-audio"
M31 = "gemini-3.1-flash-live-preview"

LOGS_1645 = [
    P25 + "표현학습 드릴 루프 감지: call_id=1645 항목=4 사유=이미 다룬 항목 재드릴 안내=1/6",
    P25 + f"표현학습 드릴 안내 주입 1/6: call_id=1645 항목=4 tc=True 모델={M25}",
    P25 + f"표현학습 드릴 안내 주입 2/6: call_id=1645 항목=6 tc=True 모델={M25}",
    P25 + f"표현학습 세트 안내 주입 1/2: call_id=1645 항목=7 tc=True 모델={M25}",
]
LOGS_1646 = [
    P25 + f"표현학습 드릴 안내 주입 1/6: call_id=1646 항목=3 tc=True 모델={M31}",
    P25 + f"표현학습 드릴 안내 주입 2/6: call_id=1646 항목=3 tc=True 모델={M31}",
]


def test_notice_items_and_per_item_cap():
    assert h.parse_drill_notices(LOGS_1645) == [4, 6]          # 감지 줄은 세지 않는다
    assert h.parse_drill_notices(LOGS_1646) == [3, 3]          # 같은 항목 2회 = 12차 상한(항목별 1회) 위반
    assert h.parse_move_on_notices(LOGS_1645) == 2             # 주입 줄만(감지 줄 제외)


def test_notice_tc_rule_complete_turn_only_on_25():
    rows_25 = h.notice_tc_rows(LOGS_1645)
    assert [(k, tc) for k, tc, _m in rows_25] == [("드릴", True), ("드릴", True), ("세트", True)]
    assert h.notice_tc_violations(rows_25) == []               # 2.5 + tc=True 는 규칙대로
    rows_31 = h.notice_tc_rows(LOGS_1646)
    assert len(h.notice_tc_violations(rows_31)) == 2           # 3.1 인데 tc=True → 12차 ②에서 버그로 확정
    assert h.notice_tc_violations([("드릴", False, M31)]) == []


def test_call_id_coverage_counts_untagged_expression_lines():
    untagged = P25 + "표현학습 가르침 판정(LLM): B12 → [] (남은 11)"
    tag, untag, kinds = h.call_id_coverage(LOGS_1645 + [untagged])
    assert (tag, untag) == (4, 1)
    assert kinds == ["표현학습 가르침 판정(LLM)"]
    assert h.call_id_coverage(LOGS_1646) == (2, 0, [])         # 12차 기대


def test_offset_expect_default_is_passed():
    """12차: 세트 밖 자발 정답도 서버가 기록해야 한다(1646 #7 미기록이 ✖ 로 잡히도록)."""
    assert h.OFFSET_EXPECT == "passed"


# ── ⑤ 반말(정중형 누락) 답 → 서버 failed (12차) ──────────────────────────── #
def _rec(iid, surface, k):
    return h.ItemRecord(item=h.Item(iid, surface, "x", "", ("x",), kind="chunk"), k=k, policy=3)


def _casual_case(server_row):
    """1645 #15 재현 — 퀴즈에서 「本当？」(반말) → 공개 뒤 복창."""
    T = h.Turn
    turns = [T(0, "learner", 1.0, "本当？", stt="本当 ?", kind="casual", item_id=15),
             T(1, "learner", 2.0, "本当ですか", stt="本当 です か ?", kind="parrot", item_id=15)]
    r = _rec(15, "本当ですか", 15)
    r.drill_answers = ["correct", "idk"]
    r.rounds.append(h.QuizRound(n=1, asked_at=1.0, block=5, revealed=True, heard=True,
                                answer_turns=[0, 1], answers=["casual", "parrot"]))
    return h.llm_judge_table({15: r}, [15], [server_row], turns, num_of={15: 15})


def test_casual_answer_must_be_recorded_failed():
    _ok, _lines, cnt = _casual_case({"item_id": 15, "surface": "本当ですか", "passed": False, "failed": True, "drilled": True})
    assert cnt["casual"] == [1, 1, []]


def test_casual_answer_passed_is_counted_as_violation():
    """1645 실측 — 서버가 반말 답을 passed 로 적었다."""
    ok, _lines, cnt = _casual_case({"item_id": 15, "surface": "本当ですか", "passed": True, "failed": False, "drilled": True})
    assert ok is False
    assert cnt["casual"][0] == 1 and cnt["casual"][1] == 0 and cnt["casual"][2] == ["15:passed"]


# ── ④-1 드릴에서 공개 전 자발 정답 → 뒤에 되감기 공개·복창 (1647 #3 こんばんは) ── #
def _spont_case(server_row, stt="こんばんは 。", windows=None):
    T = h.Turn
    turns = [T(7, "learner", 17.0, "こんばんは", stt=stt, kind="correct", item_id=3, wall=1000.0),
             T(18, "learner", 44.0, "おやすみなさい", stt="お やすみ なさい 。", kind="distractor", item_id=3, wall=1030.0),
             T(20, "learner", 48.0, "こんばんは", stt="こんばんは 。", kind="parrot", item_id=3, wall=1034.0)]
    r = h.ItemRecord(item=h.Item(3, "こんばんは", "Good evening", "", ("good evening",), kind="chunk"), k=3, policy=3)
    r.drill_attempts, r.drill_answers, r.drill_revealed = 1, ["correct"], False
    r.rounds.append(h.QuizRound(n=1, asked_at=44.0, block=1, revealed=True, heard=True,
                                answer_turns=[18, 20], answers=["distractor", "parrot"]))
    return h.llm_judge_table({3: r}, [3], [server_row], turns, num_of={3: 3},
                             server_windows=windows if windows is not None else [(1, 990.0, 1050.0, [1, 2, 3])])


PASSED_ROW = {"item_id": 3, "surface": "こんばんは", "passed": True, "drilled": True}
NOT_PASSED_ROW = {"item_id": 3, "surface": "こんばんは", "passed": False, "drilled": True}


def test_drill_spontaneous_correct_makes_server_pass_right():
    """1647 #3 — 서버 why=«학습자가 정답을 먼저 말함». ④(공개 뒤 복창)로 세면 서버가 억울하게 ✖ 가 된다."""
    ok, lines, cnt = _spont_case(PASSED_ROW)
    assert ok is True and cnt["drill_spont"] == [1, 1] and cnt["reveal"] == [0, 0]
    assert "드릴 자발 정답(공개 전)" in lines[2] and lines[2].endswith("✔ |")


def test_drill_spontaneous_correct_expects_pass_inside_server_window():
    ok, _lines, _cnt = _spont_case(NOT_PASSED_ROW)
    assert ok is False              # 창 안에서 들은 자발 정답인데 서버가 안 적었다 → ✖


def test_outside_server_window_is_ambiguous():
    ok, lines, _cnt = _spont_case(NOT_PASSED_ROW, windows=[(1, 2000.0, 2100.0, [1, 2, 3])])
    assert ok is True and lines[2].endswith("~ |")


def test_broken_transcript_drill_answer_is_not_evidence():
    """1645 #15 — 드릴 정답이 «ポンド です か ?» 로 깨졌으면 서버가 그걸로 통과를 줄 수 없다 → ④ 그대로."""
    ok, lines, cnt = _spont_case(PASSED_ROW, stt="ポンド です か ?")
    assert ok is False and cnt.get("drill_spont") is None
    assert "비버 공개 뒤 복창 ④" in lines[2]
