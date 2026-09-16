"""E2E 하네스 — 8차 재검 뒤: ① 유예 창(«세트 밖 passed + covered») 기대 ② 전사 오인식이면 ~ ③ 생존 확인 턴은 항목 질문 아님. 서버·DB 없이."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import e2e_expression_call as h  # noqa: E402

GRACE_LOGS = [
    "2026-09-16T03:17:23Z\tINFO:x:normalcall 표현학습 퀴즈 판정(LLM·세트 밖): 항목 7 «いってきます» passed + covered (U said itte kimasu)",
    "2026-09-16T03:18:27Z\tINFO:x:normalcall 표현학습 퀴즈 판정(LLM·세트 밖): 항목 10 «どうも» passed + covered ()",
    "2026-09-16T03:18:27Z\tINFO:x:normalcall 표현학습 퀴즈 판정(LLM): seq=3 U75까지 passed=[] failed=[] pending=[]",
]


def test_parse_grace_passes():
    assert h.parse_grace_passes(GRACE_LOGS) == {7, 10}
    assert h.parse_grace_passes(None) == set() and h.parse_grace_passes(["무관"]) == set()


def _rec(iid, surface, k, **kw):
    r = h.ItemRecord(item=h.Item(iid, surface, "x", "", ("x",), kind="vocab"), k=k, policy=1)
    for a, v in kw.items():
        setattr(r, a, v)
    return r


def test_grace_pass_is_ok_for_spontaneous_but_flags_repeat_after_reveal():
    T = h.Turn
    turns = [T(0, "learner", 1.0, "いってきます", stt="行っ て き ます 。", kind="correct", item_id=7),
             T(1, "learner", 2.0, "どうも", stt="どう も", kind="parrot", item_id=10)]
    spont = _rec(7, "いってきます", 1, drill_answers=["correct"])            # 공개 없이 드릴에서 자발 정답
    parrot = _rec(10, "どうも", 2, drill_answers=["idk", "parrot"], drill_revealed=True)   # 공개 뒤 복창
    qi = [{"item_id": 7, "surface": "いってきます", "passed": True, "drilled": True},
          {"item_id": 10, "surface": "どうも", "passed": True, "drilled": True}]
    num_of = {7: 7, 10: 10}
    ok, lines, cnt = h.llm_judge_table({7: spont, 10: parrot}, [7, 10], qi, turns,
                                       num_of=num_of, grace_passed={7, 10})
    assert ok is False and cnt["grace"] == [2, 1]
    assert "공개 전 자발 정답" in lines[2] and lines[2].endswith("✔ |")
    assert "공개 뒤 복창인데 서버가 유예 창에서 통과 ⛔" in lines[3] and lines[3].endswith("✖ |")


def test_stt_mismatch_makes_expectation_ambiguous():
    T = h.Turn
    turns = [T(0, "learner", 1.0, "한국요", stt="한국어", kind="correct", item_id=7)]
    r = _rec(7, "한국", 1)
    r.rounds.append(h.QuizRound(n=1, asked_at=1.0, block=2, spontaneous_correct=True, heard=True, answer_turns=[0], answers=["correct"]))
    qi = [{"item_id": 7, "surface": "한국", "passed": False, "failed": True, "drilled": True}]
    ok, lines, cnt = h.llm_judge_table({7: r}, [7], qi, turns, num_of={7: 15}, server_sets=[[4, 5, 15]])
    assert ok is True and cnt["stt_mismatch"] == 1 and lines[2].endswith("~ |")


def test_presence_check_turn_is_not_an_item_question():
    items = {1: h.Item(1, "こんにちは", "Hello", "", ("hello",), kind="chunk")}

    class _V:
        async def pcm(self, text, lang):
            return b"\0" * 320

    class _P:
        enabled = False
        calls = 0
        client = None

    sess = h.Session(items, _V(), _P(), probe=True, verbose=False, course="expression", lesson={"no": 1})
    got = asyncio.run(sess.identify("Hello? Are you there?", "", [], True, False))
    assert got == (None, "생존 확인")
    assert asyncio.run(sess.identify('How do you say "Hello"?', "", [], True, False))[0] == 1
