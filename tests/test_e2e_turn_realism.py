"""E2E 하네스 — 6차 재검(1621~1623) 뒤 수정: 끊긴 비버 턴엔 대답 안 함 · ja 방해어 · 멈춤형 첫 답 기대 ~ · 여는 문구. 서버·DB 없이."""

from __future__ import annotations

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


def _session(items=None):
    items = items or {1: h.Item(1, "こんにちは", "Hello", "", ("hello",), kind="chunk")}
    return h.Session(items, _Voice(), _Picker(), probe=False, verbose=False, course="expression", lesson={"no": 1})


def test_looks_cut_off():
    assert h.looks_cut_off("") and h.looks_cut_off("   ")
    assert h.looks_cut_off("Hahaha! You") and h.looks_cut_off("Hahaha! That's")
    assert not h.looks_cut_off("Good.")                                   # 문장부호로 끝남
    assert not h.looks_cut_off('It\'s "こんにちは"')                        # 인용 있음
    assert not h.looks_cut_off("Okay, now let's move on to the next one")  # 5어절 이상


def test_cut_off_beaver_turn_gets_no_reply_but_full_non_question_still_acks():
    sess = _session()
    assert sess.decide_reply("Hahaha! That's", [], asked=False)[0] is None
    assert sess.decide_reply("", [], asked=False)[0] is None
    assert sess.decide_reply("Alright, let's keep going.", [], asked=False) == ("Okay.", "en", "ack")


def test_ja_uses_japanese_distractors_and_no_stall_words(monkeypatch):
    monkeypatch.setattr(h, "LANGUAGE", "ja")
    sess = _session()
    sess.distractor_pool = []
    got = {sess.next_distractor() for _ in range(len(h.DISTRACTORS_JA))}
    assert got == set(h.DISTRACTORS_JA)
    assert not (set(h.DISTRACTORS) & h.STALL_WORDS) and not (set(h.DISTRACTORS_JA) & h.STALL_WORDS)


def _rec(iid, surface, k):
    return h.ItemRecord(item=h.Item(iid, surface, "x", "", ("x",), kind="vocab"), k=k, policy=5)


def test_hint_path_with_stall_first_answer_is_either_way():
    T = h.Turn
    turns = [T(0, "learner", 1.0, "잠시만요", stt="잠시만요.", kind="distractor", item_id=5),
             T(1, "learner", 2.0, "마타네", stt="また ね 。", kind="correct", item_id=5)]
    r = _rec(5, "またね", 1)
    r.rounds.append(h.QuizRound(n=1, asked_at=1.0, block=1, spontaneous_correct=True, heard=True, hint_path=True,
                                answer_turns=[0, 1], answers=["distractor", "correct"]))
    qi = [{"item_id": 100 + n, "surface": f"s{n}", "passed": False, "drilled": True} for n in range(4)]
    qi.append({"item_id": 5, "surface": "またね", "passed": True, "drilled": True})
    ok, lines, _ = h.llm_judge_table({5: r}, [5], qi, turns, server_sets=[[4, 5, 6]])
    assert ok is True and "멈춤형 첫 답" in lines[2] and lines[2].endswith("~ |")
    qi[-1]["passed"] = False
    assert h.llm_judge_table({5: r}, [5], qi, turns, server_sets=[[4, 5, 6]])[0] is True


def test_quiz_open_phrase_see_what_you_remember():
    assert h.QUIZ_RE.search("Hold on, we're not done yet. Let's see what you actually remember.")
