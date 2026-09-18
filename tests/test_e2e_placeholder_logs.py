"""E2E 하네스 — 11차 준비: ① 자리표시자 «◯◯» 를 실제 낱말로 말한다(TTS/STT 파손 방지) ② 로그를 call_id 로 가른다. 서버·DB 없이."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import e2e_expression_call as h  # noqa: E402


def test_fill_placeholder_ja_country_name_thing():
    assert h.fill_placeholder("◯◯から来ました", "ja") == "韓国から来ました"     # 1643 에서 깨지던 문장
    assert h.fill_placeholder("◯◯です", "ja") == "ジョンです"
    assert h.fill_placeholder("お名前は何ですか", "ja") == "お名前は何ですか"     # 자리표시자 없음 — 그대로


def test_fill_placeholder_ko():
    assert h.fill_placeholder("저는 ◯◯이에요", "ko") == "저는 존이에요"
    assert h.fill_placeholder("저는 ◯◯ 사람이에요", "ko") == "저는 한국 사람이에요"
    assert h.fill_placeholder("◯◯이/가 뭐예요?", "ko") == "사과가 뭐예요?"


def test_filter_logs_for_call_keeps_ours_and_untagged():
    logs = [
        "2026-09-18T08:55:47Z\tINFO:x:normalcall 표현학습 퀴즈 큐 얹기: call_id=1643 seq=1 항목=[1, 2, 3] tc=True",
        "2026-09-18T08:56:42Z\tINFO:x:normalcall 표현학습 퀴즈 큐 얹기: call_id=1644 seq=1 항목=[1, 2, 3] tc=False",
        "2026-09-18T08:56:43Z\tINFO:x:normalcall 표현학습 퀴즈 큐 열림: seq=2 항목=[4, 6, 7]",          # 태그 없는 줄은 남긴다
    ]
    keep, foreign = h.filter_logs_for_call(logs, 1643)
    assert foreign == 1 and len(keep) == 2 and all("1644" not in k for k in keep)
    assert h.filter_logs_for_call(logs, None) == (logs, 0)
    assert h.filter_logs_for_call([], 1643) == ([], 0)


def _rec_with_round(iid, answer_turns):
    r = h.ItemRecord(item=h.Item(iid, f"s{iid}", "x", "", ("x",), kind="vocab"), k=1, policy=1)
    r.rounds.append(h.QuizRound(n=1, asked_at=1.0, block=1, answer_turns=list(answer_turns)))
    return r


def test_drill_streaks_counts_consecutive_drill_turns_per_item_excluding_quiz():
    T = h.Turn
    turns = [T(0, "learner", 1.0, "a", kind="idk", item_id=5),
             T(1, "beaver", 1.5, "again"),
             T(2, "learner", 2.0, "a", kind="parrot", item_id=5),
             T(3, "learner", 3.0, "a", kind="correct", item_id=5),
             T(4, "learner", 4.0, "b", kind="correct", item_id=6),     # 다른 항목 — 런 끊김
             T(5, "learner", 5.0, "a", kind="parrot", item_id=5),
             T(6, "learner", 6.0, "q", kind="correct", item_id=7)]     # 퀴즈 회차 답 — 제외
    st = h.drill_streaks({7: _rec_with_round(7, [6])}, turns)
    assert st[5] == 3 and st[6] == 1 and 7 not in st
    assert h.drill_streaks({}, []) == {}


def test_parse_move_on_notices():
    logs = ["2026-09-19T01:00:00Z\tINFO:x:normalcall 표현학습 드릴 상한: 항목 5 학습자 턴 4 — 다음 항목으로 안내 주입 1/3",
            "2026-09-19T01:01:00Z\tINFO:x:normalcall 표현학습 퀴즈 큐 열림: seq=1 항목=[1, 2, 3]"]
    assert h.parse_move_on_notices(logs) == 1 and h.parse_move_on_notices(None) == 0
