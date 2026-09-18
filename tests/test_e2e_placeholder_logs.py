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
