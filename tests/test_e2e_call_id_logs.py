# -*- coding: utf-8 -*-
"""11차 A — «call_id=NNNN» 접두가 붙은 서버 로그를 세트·창·유예 파서가 그대로 읽는가.

1645·1646 실측: 접두가 종전 첫 필드(«seq=»/«항목») 앞에 끼면서 파서 3개가 통째로 빈값을 냈다
(리포트에 «서버 퀴즈 세트 로그 없음» 이 찍혀 세트 밖·유예 검증이 꺼진 채로 나갔다).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import e2e_expression_call as h  # noqa: E402

P = "2026-09-18T09:23:57.387300Z\tINFO:domains.learning.realtime.call_session:normalcall "

# 1645 실측 줄(접두 있음) + 옛 줄(접두 없음) 섞어 넣는다 — 서버가 아직 전 줄에 붙이지 못했다.
LOGS_11 = [
    P + "표현학습 퀴즈 큐 arm: call_id=1645 seq=1 항목=[1, 2, 3] retry=False covered=3",
    P + "표현학습 퀴즈 큐 얹기: call_id=1645 seq=1 항목=[1, 2, 3] 얹기=마이크 대기=6s tc=True 모델=gemini-live-2.5-flash-native-audio",
    P + "표현학습 퀴즈 큐 열림: call_id=1645 seq=1 open_seg=10 항목=[1, 2, 3]",
    "2026-09-18T09:24:27.403231Z\tWARNING:domains.learning.realtime.call_session:normalcall "
    "표현학습 퀴즈 큐 강제 닫힘: call_id=1645 seq=1 학습자 턴 6 상한 — 닫힘 트리거 없이 열려 있었다(1601)",
    "2026-09-18T09:24:54.597258Z\tINFO:domains.learning.realtime.call_session:normalcall "
    "표현학습 퀴즈 큐 열림: call_id=1645 seq=2 open_seg=33 항목=[4, 5, 6]",
    "2026-09-18T09:25:08.677606Z\tINFO:domains.learning.realtime.call_session:normalcall "
    "표현학습 퀴즈 닫힘(LLM 판정): seq=2 닫힘=세트 전부 확정 창=33~(6세그) set=[4, 5, 6] 확정=[4, 5, 6] 남은=[]",
    "2026-09-18T09:27:08.000000Z\tINFO:domains.learning.realtime.call_session:normalcall "
    "표현학습 퀴즈 판정(LLM·세트 밖): call_id=1645 항목 13 «いいですね» passed + covered ()",
]

LOGS_OLD = [
    P + "표현학습 퀴즈 큐 열림: seq=1 open_seg=10 항목=[1, 2, 3]",
    P + "표현학습 퀴즈 닫힘(LLM 판정): seq=1 닫힘=세트 전부 확정 창=10~(6세그) set=[1, 2, 3] 확정=[1, 2, 3] 남은=[]",
    P + "표현학습 퀴즈 판정(LLM·세트 밖): 항목 7 «◯◯から来ました» passed + covered ()",
]


def test_sets_parse_with_call_id_prefix():
    sets = h.parse_quiz_sets(LOGS_11)
    assert [(seq, nums) for _ts, seq, nums in sets] == [(1, [1, 2, 3]), (2, [4, 5, 6])]
    assert all(ts for ts, _s, _n in sets)


def test_windows_close_on_forced_and_llm_close_lines():
    wins = {seq: (op, cl, nums) for seq, op, cl, nums in h.parse_quiz_windows(LOGS_11)}
    assert set(wins) == {1, 2}
    assert wins[1][1] is not None           # 강제 닫힘 줄(접두 있음)로 닫힌다
    assert wins[2][1] is not None           # 닫힘(LLM 판정) 줄(접두 없음)로 닫힌다
    assert wins[1][1] > wins[1][0] and wins[2][1] > wins[2][0]


def test_grace_pass_number_with_and_without_prefix():
    assert h.parse_grace_passes(LOGS_11) == {13}
    assert h.parse_grace_passes(LOGS_OLD) == {7}


def test_old_lines_still_parse():
    assert [(seq, nums) for _ts, seq, nums in h.parse_quiz_sets(LOGS_OLD)] == [(1, [1, 2, 3])]
    assert h.parse_quiz_windows(LOGS_OLD)[0][2] is not None


def test_foreign_call_lines_dropped_ours_kept():
    mixed = LOGS_11 + [P + "표현학습 퀴즈 큐 열림: call_id=1646 seq=9 open_seg=3 항목=[1, 2]"]
    keep, foreign = h.filter_logs_for_call(mixed, 1645)
    assert foreign == 1
    assert [(seq, nums) for _ts, seq, nums in h.parse_quiz_sets(keep)] == [(1, [1, 2, 3]), (2, [4, 5, 6])]
    # 접두가 없는 옛 줄은 남긴다(공용 줄 · 옛 서버) — 섞임은 «목록 줄 2개» 안전판이 잡는다
    assert h.filter_logs_for_call(LOGS_OLD, 1645) == (LOGS_OLD, 0)
