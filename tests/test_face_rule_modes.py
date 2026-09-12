# -*- coding: utf-8 -*-
"""표정 툴 규칙 모드(LIVE_FACE_RULE_MODE) 회귀 — ctx-lab 2026-09-12.

지키는 것:
① 옛 모드("")는 종전과 바이트 동일 — 선언은 옛 6종 enum, [표정] 블록은 FACE_TOOL_RULE_LEGACY, 표현학습·프리토킹 대본엔 안 붙는다.
② 운영(qual, 기본값)·budget 은 enum 에 neutral 이 **없다**(앱이 클립 뒤 idle 로 스스로 돌아온다 — 사장님 확정).
③ 선언 설명과 [표정] 블록이 같은 모드를 가리킨다(«선언은 붙고 규칙은 일반 통화만» 재발 방지).
④ build_expression_instruction / build_freetalk_instruction 은 face_rule 을 주면 끝에 붙이고, 빈 값이면 바이트 동일.
⑤ call_session 의 두 코스 빌더 호출 4곳 전부 face_rule= 을 넘긴다(AST).
"""
from __future__ import annotations

import pytest

from core import gemini_live as gl
from core import persona_prompt as pp
from core.config import settings
from core.prompts.locked import face as locked_face
from core.prompts.expression import build_expression_instruction
from core.prompts.freetalk import build_freetalk_instruction

_EXPR = dict(role="비버", personality="까칠", level_profile="L1", locale="en", interests=["여행"],
             items=[{"obj": "안녕하세요", "des": "hello", "ex": None}], quiz_group=3)
_FT = dict(role="비버", personality="까칠", level_profile="L1", locale="en", interests=["여행"])


def _enum(tool):
    return list(tool.function_declarations[0].parameters.properties["emotion"].enum)


def test_production_default_is_qual():
    """운영 기본값 = qual(사장님 결정 2026-09-12). SET_FACE_TOOL 은 잠금 상수(qual)로 만든다."""
    assert settings.__class__.model_fields["LIVE_FACE_RULE_MODE"].default == "qual"
    assert "neutral" not in _enum(gl.SET_FACE_TOOL)
    assert gl.SET_FACE_TOOL.function_declarations[0].description == locked_face.SET_FACE_DESCRIPTION
    assert pp._FACE_TOOL_RULE == locked_face.FACE_TOOL_RULE


def test_legacy_mode_is_byte_identical_to_the_old_wiring(monkeypatch):
    monkeypatch.setattr(settings, "LIVE_FACE_RULE_MODE", "")
    tool = gl.set_face_tool()
    assert _enum(tool) == list(locked_face.SET_FACE_EMOTIONS_LEGACY) and "neutral" in _enum(tool)
    assert tool.function_declarations[0].description == locked_face.SET_FACE_DESCRIPTION_LEGACY
    assert pp.face_tool_rule() == locked_face.FACE_TOOL_RULE_LEGACY
    assert build_expression_instruction(**_EXPR) == build_expression_instruction(**_EXPR, face_rule="")
    assert build_freetalk_instruction(**_FT) == build_freetalk_instruction(**_FT, face_rule="")


@pytest.mark.parametrize("mode", ["budget", "qual"])
def test_budget_and_qual_have_no_neutral(monkeypatch, mode):
    monkeypatch.setattr(settings, "LIVE_FACE_RULE_MODE", mode)
    tool = gl.set_face_tool()
    assert "neutral" not in _enum(tool)
    assert set(_enum(tool)) == {"happy", "surprised", "sad", "angry", "laugh"}
    rule = pp.face_tool_rule()
    assert rule != locked_face.FACE_TOOL_RULE_LEGACY
    assert "neutral 이라는 값은 없" in rule


def test_qual_rule_has_no_numeric_budget_and_no_repeat_ban(monkeypatch):
    """사장님 최종 규칙: 횟수 상한 없음 · 같은 감정도 매번."""
    monkeypatch.setattr(settings, "LIVE_FACE_RULE_MODE", "qual")
    rule = pp.face_tool_rule()
    assert "최대" not in rule
    assert "같은 감정이어도 매번" in rule
    desc = gl.set_face_tool().function_declarations[0].description
    assert "최대" not in desc and "매번" in desc


def test_builders_append_face_rule_verbatim():
    rule = locked_face.FACE_TOOL_RULE
    e = build_expression_instruction(**_EXPR, face_rule=rule)
    f = build_freetalk_instruction(**_FT, face_rule=rule)
    assert e.endswith(rule) and f.endswith(rule)
    assert e.startswith(build_expression_instruction(**_EXPR))
    assert f.startswith(build_freetalk_instruction(**_FT))


def test_call_session_passes_face_rule_to_every_course_builder():
    """⛔ 배선 회귀(codex P1): 2차 원인은 «선언은 붙고 규칙은 일반 통화만» 이었다 — call_session 의
    build_expression_instruction / build_freetalk_instruction 호출 **전부**가 face_rule= 을 넘겨야 한다.
    빌더 단위 시험은 호출부 누락을 못 잡으므로 AST 로 호출부를 센다(legacy 2 + cur 2 = 4곳).
    """
    import ast, pathlib
    src = pathlib.Path("domains/learning/realtime/call_session.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    seen = {"build_expression_instruction": 0, "build_freetalk_instruction": 0}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in seen:
            seen[node.func.id] += 1
            kws = {k.arg for k in node.keywords}
            assert "face_rule" in kws, f"{node.func.id} 호출(줄 {node.lineno})에 face_rule= 이 없다"
    assert seen == {"build_expression_instruction": 2, "build_freetalk_instruction": 2}, seen
    # 일반 통화는 face_tool= 로 같은 규칙 블록을 붙인다(persona_prompt.build_system_instruction)
    assert "face_tool=bool(settings.LIVE_FACE_SPIKE) and wants_video" in src
