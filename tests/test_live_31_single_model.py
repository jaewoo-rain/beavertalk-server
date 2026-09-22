"""C2 — Live 모델 3.1 단일화 회귀 (D2, 2026-09-22).

옛 2.5/3.1 플랜 분기(2026-09-04)를 걷어냈다. 이 파일이 지키는 것:
  - `LIVE_MODEL_VOICE`/`LIVE_MODEL_VIDEO` 기본값이 둘 다 3.1 이고, 2.5 모델 id 문자열이
    남아 있지 않다(옛 이력을 남긴 config.py 주석은 대상이 아니다 — 실제 필드 기본값만 본다).
  - 2.5 전용 분기(`_cue_completed_turn`·`EXPR_CUE_COMPLETED_TURN_25`)가 심볼 자체로 사라졌다.
  - QA C2-②(2026-09-22): 런타임 `.py`(core·domains·main.py) 어디에도 2.5 계열 **Live
    네이티브 오디오** 모델 id 리터럴이 안 남는다. JUDGE_MODEL·CASCADE_LLM_MODEL·
    CASCADE_TTS_GEMINI_MODEL 처럼 2.5 를 그대로 쓰는 분석·캐스케이드용 설정은 D2 의
    대상이 아니라서 대상에서 뺀다(패턴이 native-audio 계열만 잡는다) — 시험 파일(tests/)
    은 역사 기록이라 애초에 스캔하지 않는다.
"""

from __future__ import annotations

import pathlib
import re

from core.config import Settings
from domains.learning.realtime import call_session as cs

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LIVE_25_MODEL_ID = re.compile(r"gemini(?:-live)?-2\.5-flash-native-audio[a-z0-9-]*", re.IGNORECASE)
_SCAN_DIRS = ("core", "domains")
_SCAN_FILES = ("main.py",)


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


def test_no_live_25_model_id_literal_in_runtime_code():
    """QA C2-②(2026-09-22) — 런타임 코드에 2.5 계열 Live 모델 id 문자열이 남으면 안 된다.
    config.py 의 주석 줄(역사 기록)만 예외 — 그 외엔 주석이든 코드든 전부 대상이다."""
    offenders: list[str] = []
    paths = [p for d in _SCAN_DIRS for p in (_ROOT / d).rglob("*.py")]
    paths += [_ROOT / f for f in _SCAN_FILES]
    for path in paths:
        rel = path.relative_to(_ROOT).as_posix()
        is_config = rel == "core/config.py"
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not _LIVE_25_MODEL_ID.search(line):
                continue
            if is_config and line.lstrip().startswith("#"):
                continue  # config.py 의 역사 기록 주석만 예외
            offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "2.5 계열 Live 모델 id 리터럴이 남아 있다:\n" + "\n".join(offenders)
