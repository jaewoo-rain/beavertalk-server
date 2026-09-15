"""E2E 하네스 4차(LLM 판정) 준비 — ① --answer-style 표기 변환 ② TTS 캐시 이름 충돌 ③ 서버 판정 결과 대조(llm_judge_table: 기대 ③④·과검출). 서버·DB 없이."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import e2e_expression_call as h  # noqa: E402


@pytest.fixture
def ja_mode(monkeypatch):
    monkeypatch.setattr(h, "LANGUAGE", "ja")
    yield


# ── 표기 변환 ───────────────────────────────────────────────────────── #
def test_hangul_romanize_and_ko_styles():
    assert h.hangul_romanize("사람요") == "saramyo"
    assert h.hangul_romanize("안녕하세요?") == "annyeonghaseyo?"
    assert h.styled_answer("사람요", "roman", "ko") == ("saramyo", "en")
    assert h.styled_answer("사람요", "hangul", "ko") is None          # ko 기본 표기


def test_kana_to_hangul_codas_and_yoon():
    assert h.kana_to_hangul("こんにちは") == "콘니치하"
    assert h.kana_to_hangul("きって") == "킷테"
    assert h.kana_to_hangul("とうきょう") == "토우쿄우"
    assert h.kana_to_hangul("カーラ") == "카라"


def test_ja_styles(ja_mode):
    assert h.styled_answer("名前です", "kana") == ("なまえです", "ja")
    assert h.styled_answer("名前です", "roman") == ("namae desu", "en")
    assert h.styled_answer("名前です", "hangul") == ("나마에데스", "ko")
    assert h.styled_answer("私はカーラです。", "roman") == ("watashi wa kaara desu", "en")   # 조사 は → wa
    assert h.styled_answer("私はカーラです。", "hangul")[0] == "와타시와카라데스。"
    assert h.styled_answer("ありがとう", "kana") is None               # 이미 가나


def test_tts_cache_name_distinguishes_non_latin_text():
    a, b = h._tts_cache_name("なまえです", "ja"), h._tts_cache_name("わたしです", "ja")
    assert a != b and a.endswith(".pcm")
    assert h._tts_cache_name("名前です", "ja") != h._tts_cache_name("名前です", "en")


# ── 서버 판정 대조 ─────────────────────────────────────────────────── #
def _rec(iid, surface, k):
    return h.ItemRecord(item=h.Item(iid, surface, "x", "", ("x",), kind="vocab"), k=k, policy=1)


def test_llm_judge_table_expectations_3_4_and_overdetection():
    T = h.Turn
    turns = [T(0, "learner", 1.0, "saramyo", tags=["표기:roman ← 사람요"], stt="saramyo", kind="correct", item_id=1),
             T(1, "learner", 2.0, "나라요", stt="나라요", kind="parrot", item_id=2)]
    r1 = _rec(1, "사람", 1)          # ③ 표기 변형 자발 정답(들림) → 서버 passed 여야
    r1.rounds.append(h.QuizRound(n=1, asked_at=1.0, block=1, spontaneous_correct=True, heard=True, answer_turns=[0], answers=["correct"]))
    r2 = _rec(2, "나라", 2)          # ④ 공개 뒤 복창 → 서버 passed 면 ✖
    r2.rounds.append(h.QuizRound(n=1, asked_at=2.0, block=1, revealed=True, answers=["parrot"], answer_turns=[1], heard=True))
    r3 = _rec(3, "고향", 3)          # ④ 드릴만 → 통과 아님
    records = {1: r1, 2: r2, 3: r3}
    qi = [{"item_id": 1, "surface": "사람", "passed": True, "failed": False, "drilled": True},
          {"item_id": 2, "surface": "나라", "passed": False, "failed": True, "drilled": True},
          {"item_id": 3, "surface": "고향", "passed": False, "failed": False, "drilled": True}]
    ok, lines, cnt = h.llm_judge_table(records, [1, 2, 3], qi, turns)
    assert ok is True and cnt["styled"] == [1, 1] and cnt["reveal"] == [1, 1] and cnt["drill_only"] == [1, 1]
    assert "saramyo → «saramyo»" in lines[2]
    # 공개 뒤 복창을 서버가 통과시키면 ✖ · 표기 변형 정답을 못 받으면 ✖ · 기록에 없는 통과는 과검출
    qi_bad = [dict(qi[0], passed=False), dict(qi[1], passed=True), qi[2], {"item_id": 9, "surface": "이름", "passed": True, "drilled": True}]
    ok2, lines2, cnt2 = h.llm_judge_table(records, [1, 2, 3], qi_bad, turns)
    assert ok2 is False and cnt2["styled"] == [1, 0] and cnt2["reveal"] == [1, 0] and cnt2["extra_passed"] == [9]
    assert lines2[-1].endswith("✖ 과검출 |")
