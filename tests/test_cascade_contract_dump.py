"""계약 문서는 **모델에서 뽑는다** — 손으로 옮기면 반드시 낡는다(2026-08-12).

프론트가 우리 문서에서 오류를 **4건** 잡았고 넷 다 사람이 옮겨 적어서 생겼다:
    ① `input_partial` → 실제 wire 값은 `input_transcript`(클래스명을 wire 로 착각)
    ② "클라에 지터버퍼 없음" → 앱엔 900ms 가 있었다(데모 화면을 앱으로 착각)
    ③ `output_transcript` 누락 → 캐스케이드가 아예 안 보내고 있었다
    ④ `ready` 필드가 camelCase 라고 적음 → 실제는 snake_case
⭐ ④ 가 특히 나쁘다: **파싱이 안 터지고 조용히 기본값으로 돈다.** 양쪽 다 "적용됐다"고 믿는다.

여기서 고정하는 성질:
  ① 문서가 **현행 모델과 일치**한다(낡으면 이 시험이 깨진다)
  ② wire `type` 값을 쓴다(클래스명이 아니라) — 오류 ①의 재발 방지
  ③ 필드 이름이 **실제로 나가는 이름**이다(별칭 반영) — 오류 ④의 재발 방지
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _collect():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "dump_cascade_contract", ROOT / "scripts" / "dump_cascade_contract.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_generated_doc_matches_the_models():
    """⛔ **문서가 낡으면 여기서 깨진다.** 그게 이 시험의 유일한 목적이다.

    깨졌다면 고칠 곳은 문서가 아니라 **모델**이고, 그다음 스크립트를 다시 돌린다.
    """
    module = _collect()
    fresh = module.collect()
    saved = json.loads((ROOT / "docs" / "cascade-contract.json").read_text(encoding="utf-8"))
    assert saved == fresh, (
        "계약 문서가 현행 모델과 다르다 — "
        "`python scripts/dump_cascade_contract.py` 를 다시 돌려라"
    )


def test_it_reports_wire_types_not_class_names():
    """② 오류 ①의 재발 방지 — `input_transcript` 이지 `ServerInputPartial` 이 아니다."""
    module = _collect()
    data = module.collect()
    wire = {m["wire_type"] for m in data["server_to_client"]}
    assert "input_transcript" in wire, sorted(wire)
    assert not [w for w in wire if w and w[0].isupper()], "클래스명이 섞였다"
    # 클래스명은 따로 적힌다(사람이 코드를 찾아갈 수 있게).
    models = {m["model"] for m in data["server_to_client"]}
    assert "ServerInputPartial" in models


def test_it_reports_the_names_that_actually_go_on_the_wire():
    """③ 오류 ④의 재발 방지 — snake_case 인지 camelCase 인지를 **모델이 답한다**."""
    module = _collect()
    data = module.collect()
    ready = next(m for m in data["server_to_client"] if m["wire_type"] == "ready")
    names = {f["name"] for f in ready["fields"]}
    assert "turn_silence_ms" in names, names
    assert not [n for n in names if any(c.isupper() for c in n)], "camelCase 로 나간다고 적혔다"


def test_frames_the_frontend_asked_about_are_present():
    """프론트가 실제로 물어본 프레임들이 문서에 있다(누락이 오류 ③이었다)."""
    module = _collect()
    wire = {m["wire_type"] for m in module.collect()["server_to_client"]}
    for needed in ("turn_start", "turn_end", "sentence", "call_ended", "output_transcript"):
        assert needed in wire, (needed, sorted(wire))


def test_the_doc_is_not_hand_written():
    """⛔ 손으로 쓴 표를 이 파일에 넣지 마라 — 생성물이라는 표시가 있어야 한다."""
    md = (ROOT / "docs" / "cascade-contract.md").read_text(encoding="utf-8")
    assert "자동 생성" in md and "손으로 고치지 마라" in md
