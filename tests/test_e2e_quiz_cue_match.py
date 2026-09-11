"""E2E 하네스의 «서버 퀴즈 큐 ↔ 비버 앵커» 시간 대조(순수 함수) — 가짜 gcloud 로그 줄로 검증.

T16(서버가 «[시스템] 지금 퀴즈» 큐를 얹어 퀴즈를 연다)이 배포되면 `scripts/e2e_expression_call.py --logs` 가
서버 로그의 큐 줄과 하네스가 문구로 잡은 앵커 턴을 시간으로 맞춰 «큐→앵커 지연» 과 «큐 없이 난 앵커» 를 센다.
⚠ 큐 로그 문구는 expr-build 가 정한다 — 접두 `QUIZ_CUE_LOG_PREFIX` 가 실제 call_session.py 문구와 같아야 한다.
  구현 뒤 그 문구가 다르면 이 상수 하나만 맞춘다(이 시험은 상수를 통해 문구를 읽으므로 그대로 산다).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import e2e_expression_call as h  # noqa: E402

T0 = 1_757_600_000.0  # 임의 epoch (2026-09)


def _ts(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _line(epoch: float, payload: str) -> str:
    return f"{_ts(epoch)}\tINFO:domains.learning.realtime.call_session:{payload}"


def test_parse_quiz_cues_keeps_only_cue_lines_with_timestamps():
    lines = [
        _line(T0 + 10, h.QUIZ_CUE_LOG_PREFIX + " 얹기: seq=1 항목=[1, 2, 3]"),
        _line(T0 + 11, "normalcall 표현학습 판정 계측: phase=drill"),          # 다른 표현학습 줄 — 큐 아님
        "(로그 없음) ",                                                          # 시각 없는 줄 — 버림
        _line(T0 + 200, h.QUIZ_CUE_LOG_PREFIX + " 얹기: seq=2 항목=[4, 5, 6]"),
        _line(T0 + 100, h.QUIZ_CUE_LOG_PREFIX + " 얹기: seq=2 항목=[2] retry"),               # 순서 섞임 — 시간순 정렬
    ]
    cues = h.parse_quiz_cues(lines)
    assert [round(t - T0) for t, _ in cues] == [10, 100, 200]
    assert all(h.QUIZ_CUE_LOG_PREFIX in p for _, p in cues)


def test_parse_accepts_second_precision_timestamp():
    lines = ["2026-09-11T09:29:46Z\tINFO:x:" + h.QUIZ_CUE_LOG_PREFIX + " 얹기"]
    assert len(h.parse_quiz_cues(lines)) == 1


def test_match_pairs_each_cue_with_first_anchor_inside_window():
    cues = [(T0 + 10.0, "c1"), (T0 + 200.0, "c2")]
    anchors = [(8, T0 + 13.4), (30, T0 + 205.0)]
    m = h.match_quiz_cues(cues, anchors)
    assert m["pairs"] == [(T0 + 10.0, 8, 3.4), (T0 + 200.0, 30, 5.0)]
    assert m["cues_without_anchor"] == []
    assert m["anchors_without_cue"] == []


def test_anchor_without_cue_and_cue_without_anchor():
    cues = [(T0 + 10.0, "c1"), (T0 + 300.0, "c2")]
    anchors = [(8, T0 + 12.0), (20, T0 + 150.0)]          # 턴 20 = 비버가 혼자 퀴즈를 열었다 · c2 = 큐 뒤 앵커 없음
    m = h.match_quiz_cues(cues, anchors)
    assert m["pairs"] == [(T0 + 10.0, 8, 2.0)]
    assert m["anchors_without_cue"] == [20]
    assert m["cues_without_anchor"] == [T0 + 300.0]


def test_anchor_before_cue_or_outside_window_is_not_matched():
    cues = [(T0 + 100.0, "c1")]
    anchors = [(5, T0 + 99.0), (9, T0 + 100.0 + h.QUIZ_CUE_MATCH_WINDOW_S + 1)]
    m = h.match_quiz_cues(cues, anchors)
    assert m["pairs"] == []
    assert m["cues_without_anchor"] == [T0 + 100.0]
    assert sorted(m["anchors_without_cue"]) == [5, 9]


def test_one_anchor_is_not_reused_for_two_cues():
    cues = [(T0 + 10.0, "c1"), (T0 + 12.0, "c2")]         # 큐가 두 번 얹혔는데 비버는 한 번만 열었다
    anchors = [(8, T0 + 15.0)]
    m = h.match_quiz_cues(cues, anchors)
    assert m["pairs"] == [(T0 + 10.0, 8, 5.0)]
    assert m["cues_without_anchor"] == [T0 + 12.0]


def test_only_attach_stage_lines_count_as_cues_by_default():
    lines = [
        _line(T0 + 1, h.QUIZ_CUE_LOG_PREFIX + " arm: seq=1 항목=[1, 2, 3] retry=False covered=3"),
        _line(T0 + 4, h.QUIZ_CUE_LOG_PREFIX + " 얹기: seq=1 항목=[1, 2, 3] 얹기=마이크 대기=3s 비버턴=(열린 턴 없음)"),
        _line(T0 + 7, h.QUIZ_CUE_LOG_PREFIX + " 열림: seq=1 open_seg=12 항목=[1, 2, 3]"),
    ]
    cues = h.parse_quiz_cues(lines)
    assert [round(t - T0) for t, _ in cues] == [4]                       # 얹기만
    assert len(h.parse_quiz_cues(lines, "arm")) == 1
    assert len(h.parse_quiz_cues(lines, "열림")) == 1
    assert len(h.parse_quiz_cues(lines, "")) == 3
