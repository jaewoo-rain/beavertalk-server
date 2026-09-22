"""C2 — Live 모델 3.1 단일화 회귀 (D2, 2026-09-22).

옛 2.5/3.1 플랜 분기(2026-09-04)를 걷어냈다. 이 파일이 지키는 것:
  - `LIVE_MODEL_VOICE`/`LIVE_MODEL_VIDEO` 기본값이 둘 다 3.1 이고, 2.5 모델 id 문자열이
    남아 있지 않다(옛 이력을 남긴 config.py 주석은 대상이 아니다 — 실제 필드 기본값만 본다).
  - 2.5 전용 분기(`_cue_completed_turn`·`EXPR_CUE_COMPLETED_TURN_25`)가 심볼 자체로 사라졌다.
"""

from __future__ import annotations

from core.config import Settings
from domains.learning.realtime import call_session as cs


def test_live_model_defaults_are_31_with_no_25_literal():
    s = Settings.model_construct()
    for field in ("GEMINI_LIVE_MODEL", "LIVE_MODEL_VOICE", "LIVE_MODEL_VIDEO"):
        value = getattr(s, field)
        assert value == "gemini-3.1-flash-live-preview", (field, value)
        assert "2.5" not in value, (field, value)
    # 3.1 은 Vertex 에 없다 — 두 Vertex 모델 필드 모두 비어 있어야 한다.
    assert s.LIVE_MODEL_VOICE_VERTEX == "" and s.LIVE_MODEL_VIDEO_VERTEX == ""


def test_25_only_branch_symbols_are_gone():
    """⛔ 되살리지 마라 — 2.5 계열 모델 자체가 더 이상 안 쓰인다(3.1 단일화)."""
    assert not hasattr(cs, "_cue_completed_turn")
    assert "EXPR_CUE_COMPLETED_TURN_25" not in Settings.model_fields
