"""E2E 하네스 순수 함수 — ① 이어하기 검사(check_resume) · ② 통과 항목 재드릴(count_redrills). 서버·DB 없이 가짜 자료로."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import e2e_expression_call as h  # noqa: E402


def _items():
    return {
        1: h.Item(1, "사람", "person", "", ("person",), kind="vocab", example="그 사람은 선생님이 아닙니다."),
        2: h.Item(2, "나라", "country", "", ("country",), kind="vocab", example="한국은 아름다운 나라예요."),
        3: h.Item(3, "N은/는 N이에요/예요", "N is N", "", ("n is n",), kind="grammar", example="저는 학생이에요."),
    }


def _turn(n, role, t, text):
    return h.Turn(n, role, t, text)


# ── ② 재드릴 ────────────────────────────────────────────────────────────── #
def test_redrill_counts_beaver_reuse_of_already_passed_item():
    items = _items()
    turns = [
        _turn(0, "beaver", 1.0, 'Say "사람".'),                         # 통화 전 통과 항목을 다시 꺼냄(표면형) → 1
        _turn(1, "learner", 2.0, "사람요"),
        _turn(2, "beaver", 3.0, "Right, 사람. Now, how do you say country?"),   # 학습자가 먼저 말한 사람 을 되받음 → 제외
        _turn(3, "learner", 4.0, "I don't know."),
        _turn(4, "beaver", 5.0, "It's 나라. Try 저는 학생이에요."),     # 나라: 이 통화에서 t=4 에 통과(passed_times) → 5.0 > 4.0 → 재드릴 · 문법 예문 → 통화 전 통과 → 재드릴
    ]
    n, ev = h.count_redrills(turns, items, passed_before={1, 3}, passed_times={2: 4.0})
    assert (0, 1) in ev
    assert (2, 1) not in ev              # 학습자가 먼저 꺼낸 경우 제외
    assert (4, 2) in ev and (4, 3) in ev
    assert n == 3


def test_redrill_ignores_items_not_yet_passed_and_counts_streak_once():
    items = _items()
    turns = [
        _turn(0, "beaver", 1.0, 'Say "나라".'),       # 통과 전 → 0
        _turn(1, "beaver", 2.0, 'Again: "나라".'),    # 연속 비버 턴 — 같은 회차
        _turn(2, "learner", 3.0, "I don't know."),
        _turn(3, "beaver", 4.0, 'Once more: "사람".'),  # 통과 항목 → 1
        _turn(4, "beaver", 5.0, '"사람"!'),             # 연속 → 1회로
    ]
    n, ev = h.count_redrills(turns, items, passed_before={1}, passed_times={})
    assert n == 1 and ev == [(3, 1)]


# ── ① 이어하기 ─────────────────────────────────────────────────────────── #
def test_check_resume_pass_when_same_call_and_ordering_and_saved_twice():
    t1 = datetime(2026, 9, 13, 10, 0, 0)
    seg1 = {"call_id": 100, "course": "expression", "passed_ids": [1, 2], "failed_ids": [3], "mi_updated_max": t1}
    seg2 = {"call_id": 100, "course_from_server": "expression", "resumed": True, "drilled_order": [3, 5, 6], "quizzed_ids": [3],
            "fragment_count": 2, "recorded_fragment": 2, "mi_updated_max": t1 + timedelta(minutes=4)}
    rows = h.check_resume(seg1, seg2, plan_fragments=3)
    assert [r[3] for r in rows] == [True, True, True, True], rows


def test_check_resume_flags_reappearance_and_missing_second_record():
    t1 = datetime(2026, 9, 13, 10, 0, 0)
    seg1 = {"call_id": 100, "course": "expression", "passed_ids": [1, 2], "failed_ids": [3], "mi_updated_max": t1}
    seg2 = {"call_id": 100, "course_from_server": "expression", "resumed": True, "drilled_order": [5, 1, 3], "quizzed_ids": [],
            "fragment_count": 2, "recorded_fragment": 1, "mi_updated_max": t1}
    rows = h.check_resume(seg1, seg2, plan_fragments=3)
    by = {r[0]: r for r in rows}
    assert by["(b) 통과 항목 재등장 없음"][3] is False      # 1 이 다시 나왔다
    assert by["(c) 오답 항목 앞줄"][3] is False               # 3 이 맨 앞이 아니다
    assert by["(d) 조각별 저장"][3] is False                  # recorded_fragment 1 · 갱신 없음


def test_check_resume_free_plan_expects_rejection():
    seg1 = {"call_id": 100, "course": "expression", "passed_ids": [], "failed_ids": [], "mi_updated_max": None}
    seg2 = {"call_id": 101, "course_from_server": "expression", "resumed": False, "drilled_order": [], "quizzed_ids": [],
            "fragment_count": 1, "recorded_fragment": 1, "mi_updated_max": None}
    rows = h.check_resume(seg1, seg2, plan_fragments=1)
    assert rows[0][3] is True and "거절" in rows[0][1]
    assert all(r[3] for r in rows)
    # 거절돼야 하는데 이어졌다면 FAIL
    seg2b = dict(seg2, call_id=100, resumed=True)
    assert h.check_resume(seg1, seg2b, plan_fragments=1)[0][3] is False
