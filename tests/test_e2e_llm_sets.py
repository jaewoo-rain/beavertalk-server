"""E2E 하네스 — 4차 재검(1615·1616·1617) 뒤 수정: 서버 퀴즈 세트 파싱 · 세트 밖 자발 정답은 미판정이 정상 · 퀴즈 여는 문구. 서버·DB 없이."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import e2e_expression_call as h  # noqa: E402

LOGS = [
    "2026-09-15T05:45:32.035733Z\tINFO:domains.learning.realtime.call_session:normalcall 표현학습 퀴즈 큐 arm: seq=4 항목=[10, 11, 12] retry=False covered=12",
    "2026-09-15T05:44:47.768947Z\tINFO:domains.learning.realtime.call_session:normalcall 표현학습 퀴즈 큐 열림: seq=3 open_seg=58 항목=[7, 8, 9]",
    "2026-09-15T05:45:43.730089Z\tINFO:domains.learning.realtime.call_session:normalcall 표현학습 퀴즈 큐 열림: seq=4 open_seg=74 항목=[10, 11, 12]",
    "(로그 없음)",
]


def test_parse_quiz_sets_only_open_lines_with_epoch():
    sets = h.parse_quiz_sets(LOGS)
    assert [(seq, nums) for _, seq, nums in sets] == [(3, [7, 8, 9]), (4, [10, 11, 12])]
    assert abs(sets[1][0] - sets[0][0] - 55.961142) < 1e-3
    assert h.parse_quiz_sets(None) == [] and h.parse_quiz_sets(["no ts 퀴즈 큐 열림: seq=1 항목=[1]"])[0] == (None, 1, [1])


def _rec(iid, surface, k):
    return h.ItemRecord(item=h.Item(iid, surface, "x", "", ("x",), kind="vocab"), k=k, policy=1)


def test_off_set_spontaneous_correct_is_not_a_judge_failure():
    T = h.Turn
    turns = [T(0, "learner", 1.0, "どうも", stt="どう も", kind="correct", item_id=10),
             T(1, "learner", 2.0, "ごめんなさい", stt="ごめん なさい 。", kind="correct", item_id=13)]
    r10 = _rec(10, "どうも", 1)
    r10.rounds.append(h.QuizRound(n=1, asked_at=1.0, block=4, spontaneous_correct=True, heard=True, answer_turns=[0], answers=["correct"]))
    r13 = _rec(13, "ごめんなさい", 2)
    r13.rounds.append(h.QuizRound(n=1, asked_at=2.0, block=4, spontaneous_correct=True, heard=True, answer_turns=[1], answers=["correct"]))
    records = {10: r10, 13: r13}
    qi = [{"item_id": 100 + n, "surface": f"s{n}", "passed": False, "drilled": True} for n in range(9)]   # 번호 1~9 자리 채움
    qi += [{"item_id": 10, "surface": "どうも", "passed": True, "drilled": True},        # 번호 10
           {"item_id": 11, "surface": "x", "passed": False, "drilled": True},
           {"item_id": 12, "surface": "y", "passed": False, "drilled": True},
           {"item_id": 13, "surface": "ごめんなさい", "passed": False, "drilled": True}]  # 번호 13 — 세트 [10,11,12] 밖
    ok, lines, cnt = h.llm_judge_table(records, [10, 13], qi, turns, server_sets=[[10, 11, 12]])
    assert ok is True and cnt["off_set"] == [13]
    assert "세트 밖" in lines[3] and lines[3].endswith("✔ |")
    # 로그가 없으면(server_sets None) 종전 기대 — 세트 밖을 모르니 미통과는 ✖
    ok2, _, cnt2 = h.llm_judge_table(records, [10, 13], qi, turns)
    assert ok2 is False and cnt2["off_set"] == []


def test_quiz_open_phrases_1615_1617():
    assert h.QUIZ_RE.search("Now, let's see if you actually learned anything. How do you say \"Goodbye\"?")
    assert h.QUIZ_RE.search("Okay, let's see if you remember anything. How do you say \"Good morning\"?")
    assert h.QUIZ_RE.search("Let's check what you've learned so far.")
    assert not h.QUIZ_RE.search("Now, how do you say \"person\" in Korean? Try it!")


def test_picker_listing_includes_examples_for_grammar_items():
    g = h.Item(10638, "N은/는 N이에요/예요", "N is N (informal polite copula)", "", ("n is n",), kind="grammar",
               example="생일이 언제예요?", examples=("생일이 언제예요?", "이 옷은 얼마예요?", "저 사람은 가수예요.", "넷째"))
    v = h.Item(24, "사람", "person", "", ("person",), kind="vocab")
    out = h.picker_listing([g, v]).splitlines()
    assert out[0] == "1. N은/는 N이에요/예요 — N is N (informal polite copula) — examples: 생일이 언제예요? / 이 옷은 얼마예요? / 저 사람은 가수예요."
    assert out[1] == "2. 사람 — person"


def test_offset_expect_passed_requires_server_pass_after_5th_deploy():
    T = h.Turn
    turns = [T(0, "learner", 1.0, "ごめんなさい", stt="ごめん なさい 。", kind="correct", item_id=13)]
    r13 = _rec(13, "ごめんなさい", 1)
    r13.rounds.append(h.QuizRound(n=1, asked_at=1.0, block=1, spontaneous_correct=True, heard=True, answer_turns=[0], answers=["correct"]))
    qi = [{"item_id": 100 + n, "surface": f"s{n}", "passed": False, "drilled": True} for n in range(12)]
    qi.append({"item_id": 13, "surface": "ごめんなさい", "passed": False, "drilled": True})     # 번호 13
    ok, _, cnt = h.llm_judge_table({13: r13}, [13], qi, turns, server_sets=[[10, 11, 12]], offset_expect="passed")
    assert ok is False and cnt["off_set"] == [13]
    qi[-1]["passed"] = True
    assert h.llm_judge_table({13: r13}, [13], qi, turns, server_sets=[[10, 11, 12]], offset_expect="passed")[0] is True


WIN_LOGS = [
    "2026-09-15T06:18:01.273991Z\tINFO:x:normalcall 표현학습 퀴즈 큐 열림: seq=2 open_seg=32 항목=[4, 5, 6]",
    "2026-09-15T06:18:21.000000Z\tINFO:x:normalcall 표현학습 퀴즈 닫힘(LLM 판정): seq=2 닫힘=다음 항목 소개 창=32~(9세그) set=[4, 5, 6] 확정=[4, 5, 6] 남은=[]",
    "2026-09-15T06:18:50.718691Z\tINFO:x:normalcall 표현학습 퀴즈 큐 열림: seq=3 open_seg=52 항목=[7, 8, 9]",
    "2026-09-15T06:19:20.000000Z\tWARNING:x:normalcall 표현학습 퀴즈 큐 강제 닫힘: seq=3 학습자 턴 6 상한 — 닫힘 트리거 없이 열려 있었다(1601)",
    "2026-09-15T06:19:20.500000Z\tINFO:x:normalcall 표현학습 퀴즈 닫힘(LLM 판정): seq=3 닫힘=학습자 턴 상한 6",
    "2026-09-15T06:26:47.000000Z\tINFO:x:normalcall 표현학습 퀴즈 큐 열림: seq=4 open_seg=34 항목=[10, 11, 12]",
]


def test_parse_quiz_windows_open_close_forced_and_open_ended():
    w = h.parse_quiz_windows(WIN_LOGS)
    assert [(seq, nums) for seq, _, _, nums in w] == [(2, [4, 5, 6]), (3, [7, 8, 9]), (4, [10, 11, 12])]
    assert abs(w[0][2] - w[0][1] - 19.726) < 1e-2          # 닫힘(LLM 판정)
    assert abs(w[1][2] - w[1][1] - 29.281) < 1e-2          # 강제 닫힘이 먼저
    assert w[2][2] is None                                   # 끝까지 열림
    t_open3 = w[1][1]
    assert h.in_quiz_window(t_open3 + 10, w) == 3
    assert h.in_quiz_window(w[1][2] + 7, w) is None          # 1618 #10: 강제 닫힘 7초 뒤 질문 — 창 밖
    assert h.in_quiz_window(w[2][1] + 3600, w) == 4


def test_hint_path_only_correct_is_expected_failed_under_5th_b():
    T = h.Turn
    turns = [T(0, "learner", 1.0, "알겠어요", stt="알겠어요?", kind="distractor", item_id=4),
             T(1, "learner", 2.0, "さようなら", stt="さようなら", kind="correct", item_id=4)]
    r = _rec(4, "さようなら", 1)
    r.rounds.append(h.QuizRound(n=1, asked_at=1.0, block=2, spontaneous_correct=True, heard=True, hint_path=True,
                                answer_turns=[0, 1], answers=["distractor", "correct"]))
    qi = [{"item_id": 100 + n, "surface": f"s{n}", "passed": False, "drilled": True} for n in range(3)]
    qi.append({"item_id": 4, "surface": "さようなら", "passed": False, "failed": True, "drilled": True})
    ok, lines, cnt = h.llm_judge_table({4: r}, [4], qi, turns, server_sets=[[4, 5, 6]])
    assert ok is True and cnt.get("hint_fail") == 1 and "첫 답 오답" in lines[2]
    qi[-1]["passed"] = True
    assert h.llm_judge_table({4: r}, [4], qi, turns, server_sets=[[4, 5, 6]])[0] is False
