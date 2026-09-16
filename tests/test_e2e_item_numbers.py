"""E2E 하네스 — 8차 준비(bt-back 2026-09-16): ① 번호 정본 = 서버 «표현학습 목록» 로그 ② 재드릴은 드릴이 끝난 뒤 재질문만 ③ 지시대명사 키워드 차단."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import e2e_expression_call as h  # noqa: E402

LOGS = [
    "2026-09-16T07:48:00.100000Z\tINFO:x:normalcall 표현학습 목록: 1=인사말 2=N은/는 N이에요/예요 3=N입니까?, N입니다 4=사람 15=한국",
    "2026-09-16T07:49:07.000000Z\tINFO:x:normalcall 표현학습 퀴즈 큐 열림: seq=1 open_seg=10 항목=[1, 2, 3]",
]


def _items():
    return {10: h.Item(10, "인사말", "Greetings", "", ("greet",), kind="grammar"),
            11: h.Item(11, "N은/는 N이에요/예요", "N is N", "", ("n is n",), kind="grammar"),
            12: h.Item(12, "N입니까?, N입니다", "Is it N?", "", ("is it n",), kind="grammar"),
            13: h.Item(13, "사람", "person", "", ("person",), kind="vocab"),
            14: h.Item(14, "한국", "Korea", "", ("korea",), kind="vocab")}


def test_parse_item_numbers_keeps_surfaces_with_spaces_and_commas():
    got = h.parse_item_numbers(LOGS)
    assert got == {1: "인사말", 2: "N은/는 N이에요/예요", 3: "N입니까?, N입니다", 4: "사람", 15: "한국"}
    assert h.parse_item_numbers(None) == {} and h.parse_item_numbers(["무관한 줄"]) == {}


def test_item_numbers_by_id_maps_server_numbers_not_snapshot_positions():
    by_id = h.item_numbers_by_id(h.parse_item_numbers(LOGS), _items())
    assert by_id == {10: 1, 11: 2, 12: 3, 13: 4, 14: 15}       # 한국 = 서버 #15 (스냅샷 위치로는 5였다 — 1626)
    dup = dict(_items()); dup[99] = h.Item(99, "사람", "person(2)", "", ("person",), kind="vocab")
    assert 13 not in h.item_numbers_by_id({4: "사람"}, dup)     # 표면형 중복이면 버린다


def test_llm_judge_table_uses_server_numbering_for_off_set():
    T = h.Turn
    turns = [T(0, "learner", 1.0, "한국요", stt="한국요?", kind="correct", item_id=14)]
    r = h.ItemRecord(item=_items()[14], k=1, policy=1)
    r.rounds.append(h.QuizRound(n=1, asked_at=1.0, block=1, spontaneous_correct=True, heard=True, answer_turns=[0], answers=["correct"]))
    qi = [{"item_id": 14, "surface": "한국", "passed": True, "drilled": True}]      # 스냅샷 위치로는 #1
    num_of = h.item_numbers_by_id(h.parse_item_numbers(LOGS), _items())
    ok, lines, cnt = h.llm_judge_table({14: r}, [14], qi, turns, server_sets=[[4, 5, 15]], num_of=num_of)
    assert ok is True and cnt["off_set"] == []                  # #15 는 세트 안 — 정본 번호를 쓰면 통과
    ok2, _, cnt2 = h.llm_judge_table({14: r}, [14], qi, turns, server_sets=[[4, 5, 15]])
    assert cnt2["off_set"] == [1]                               # 폴백(스냅샷 위치)이면 세트 밖으로 잘못 본다


def test_redrill_counts_only_reask_after_drill_completed():
    items = {1: h.Item(1, "こんにちは", "Hello", "", ("hello",), kind="chunk")}
    sess = h.Session(items, None, None, probe=True, verbose=False, course="expression", lesson={"no": 1})
    r = h.ItemRecord(item=items[1], k=1, policy=1)
    sess.records[1] = r
    sess.drilled_order.append(1)
    r.drill_answers.append("idk")
    r.rounds.append(h.QuizRound(n=1, asked_at=5.0, anchored=False))      # 드릴 중 재질문 — 재드릴 아님
    assert r.drill_done_at is None
    r.drill_done_at = 10.0
    r.rounds.append(h.QuizRound(n=2, asked_at=12.0, anchored=False))     # 드릴 끝난 뒤 재질문 — 재드릴
    after = [rd for rd in r.rounds if not rd.anchored and rd.asked_at > (r.drill_done_at or 1e9)]
    assert len(after) == 1 and after[0].n == 2


def test_generic_keywords_are_not_item_keywords():
    assert "one" not in h.keywords_for("이", "this one", "vocab")
    assert "that" not in h.keywords_for("그", "that", "vocab")
    assert h.keywords_for("사람", "person", "vocab") == ("person",)
