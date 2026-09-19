# -*- coding: utf-8 -*-
"""14차 계기 — ①학습자 턴 없이 이어진 비버 턴(1651 자문자답) ②«안내 보류» 로그 ③턴 길이 비교.

1651(사장님 통화): 13차 D 로 같은 비버 턴에 세트 이탈이 두 번 감지돼 안내가 5초 간격으로 2회 주입됐고,
비버가 그걸 대화 차례로 받아 학습자 턴 없이 t13→t14→t15 로 혼자 묻고 답했다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import e2e_expression_call as h  # noqa: E402

T = h.Turn


def _b(n, text, tags=None):
    return T(n, "beaver", float(n), text, tags=tags or [])


def _u(n, text="はい"):
    return T(n, "learner", float(n), text, stt=text, kind="correct")


def test_1651_shape_three_beaver_turns_in_a_row():
    turns = [_u(12), _b(13, 'Now, how do you say "Thank you"?'),
             _b(14, "지금 낼 문제는 1·2·3 뿐이야. 다시 — how do you say it?"),
             _b(15, "It's ありがとうございます.", tags=["표면형:ありがとうございます(reveal) 퀴즈공개"]), _u(16)]
    runs = h.beaver_solo_runs(turns)
    assert len(runs) == 1 and len(runs[0]) == 3
    assert [t.n for t in runs[0]] == [13, 14, 15]
    assert h.solo_run_self_answered(runs[0]) is True


def test_empty_beaver_turn_does_not_make_a_run():
    turns = [_u(1), _b(2, "Now, how do you say \"Hello\"?"), _b(3, "  "), _u(4)]
    assert h.beaver_solo_runs(turns) == []


def test_learner_turn_breaks_the_run():
    turns = [_b(1, "A"), _u(2), _b(3, "B"), _u(4), _b(5, "C")]
    assert h.beaver_solo_runs(turns) == []


def test_rhetorical_question_is_not_self_answering():
    """1638 t15 — 「Are you naming foods now?!」 뒤에 공개가 와도 자문자답이 아니다(수사의문문)."""
    run = [_b(15, "[폭소] Are you naming foods now?! It's さようなら."),
           _b(16, "さようなら.", tags=["표면형:さようなら(reveal) 퀴즈공개"])]
    assert h.beaver_solo_runs([_u(14)] + run + [_u(17)])          # 묶음 자체는 잡힌다
    assert h.solo_run_self_answered(run) is False                  # 자문자답은 아니다


def test_two_turn_run_that_asks_then_reveals_is_self_answering():
    run = [_b(20, 'Now, how do you say "Good evening"?'),
           _b(21, "It's こんばんは.", tags=["표면형:こんばんは(reveal) 퀴즈공개"])]
    assert h.solo_run_self_answered(run) is True


P = "2026-09-19T01:02:03Z\tINFO:domains.learning.realtime.call_session:normalcall "


def test_parse_notice_holds_by_reason():
    logs = [P + "표현학습 세트 안내 보류: call_id=1660 seq=3 이유=같은 턴에 이미 주입",
            P + "표현학습 세트 안내 보류: call_id=1660 seq=3 이유=주입 간격 30s 미만",
            P + "표현학습 드릴 안내 보류: call_id=1660 항목=4 사유=창 연 직후 15s",
            P + "표현학습 세트 안내 보류: call_id=1660 seq=4",
            P + "표현학습 퀴즈 큐 보류: call_id=1660 seq=1 항목=[1, 2, 3] 이유=항목 3 미정리(학습자 턴 0/3)"]
    holds = h.parse_notice_holds(logs)
    assert sum(holds.values()) == 4                      # 큐 보류는 안 센다
    assert holds.get("세트 같은 턴에 이미 주입") == 1
    assert holds.get("세트 주입 간격 30s 미만") == 1
    assert holds.get("드릴 창 연 직후 15s") == 1
    assert holds.get("세트 (사유 없음)") == 1
    assert sum(v for k, v in holds.items() if k.startswith("드릴")) == 1
    assert h.parse_notice_holds([]) == {}
