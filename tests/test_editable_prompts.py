"""편집 가능한 프롬프트(core/prompts/editable/*.md) 로더 — 파싱·검사·폴백 · 기본판 정합 · 조립 바이트 동일(2026-09-12 잠금/편집 분리).

조립 결과의 바이트 동일은 각 코스 시험의 기준 해시가 지킨다(test_persona_prompt·test_prompt_common_snapshot(94)·test_expression_prompt::_EXPR_FROZEN·
test_freetalk_lesson::_FREETALK_OLD_FROZEN). 여기서는 로더 자체와 «편집 파일 == 기본판 스냅샷»(커밋 시점) 을 잠근다.
"""
from __future__ import annotations

import logging
import os

import pytest

from core.prompts import editable_loader as el

NAMES = ("normal", "expression", "freetalk", "leveltest")


@pytest.fixture(autouse=True)
def _fresh_cache():
    el.reset_cache()
    yield
    el.reset_cache()


# --------------------------------------------------------------------------- #
# 기본판 정합
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", NAMES)
def test_editable_file_equals_its_default_snapshot_at_commit_time(name: str) -> None:
    """커밋 시점엔 편집 파일과 기본판이 같다 — 기본판은 «지금 문구 스냅샷» 이다. 편집 파일을 고치면 기본판도 같이 갱신하는 게 규율(EDITING.md)."""
    with open(os.path.join(el.EDITABLE_DIR, f"{name}.md"), encoding="utf-8") as f:
        a = el.parse(f.read())
    with open(os.path.join(el.EDITABLE_DIR, f"{name}.default.md"), encoding="utf-8") as f:
        b = el.parse(f.read())
    assert a == b
    assert el.validate(name, a, b) == []


@pytest.mark.parametrize("name", NAMES)
def test_default_fits_its_token_budget_with_headroom(name: str) -> None:
    sections = el.load(name)
    est = el.estimate_tokens("\n".join(sections.values()))
    budget = el.TOKEN_BUDGET[name]
    assert est <= budget, f"{name}: 근사 {est} > 예산 {budget}"
    assert est >= budget * 0.5, f"{name}: 예산이 너무 느슨하다({est} vs {budget}) — 한 줄 늘어도 안 잡힌다"


def test_expected_sections_exist() -> None:
    assert set(el.load("normal")) >= {"persona_intro", "rule1_head", "rule1_mode_default", "rule1_mode_checkboard", "rule1_tail", "rule3", "rule4",
                                       "lang_policy_survival", "lang_policy_beginner", "lang_policy_intermediate", "lang_policy_advanced",
                                       "seed_opening", "seed_opening_lean"}
    assert set(el.load("expression")) == {"persona_intro", "rule1", "rule3_codeswitch", "rule4", "drill_intro", "seed_opening"}
    assert set(el.load("freetalk")) >= {"persona_intro_old", "rule1_old", "rule3_old", "rule4_old", "seed_opening_old", "persona_intro_lesson",
                                         "rule1_lesson", "rule3_lesson", "rule4_lesson", "seed_opening_lesson", "lesson_header",
                                         "lesson_partner_line", "lesson_partner_fallback", "lesson_material_line", "lesson_probes_prefix"}
    assert set(el.load("leveltest")) == {"intro", "seed_opening"}


def test_parse_strips_html_comment_and_keeps_indentation() -> None:
    text = "<!-- 안내\n여러 줄 -->\n## a\n\n   - 들여쓴 불릿\n둘째 줄\n\n## b\n본문 b\n"
    assert el.parse(text) == {"a": "   - 들여쓴 불릿\n둘째 줄", "b": "본문 b"}
    assert el.parse("## a\r\nx\r\n") == {"a": "x"}, "CRLF 체크아웃도 같은 결과"


# --------------------------------------------------------------------------- #
# 검사 — 슬롯·금지어·대괄호·토큰
# --------------------------------------------------------------------------- #
def test_validate_catches_each_violation() -> None:
    default = el.load("expression")
    ok = dict(default)
    assert el.validate("expression", ok, default) == []

    missing_slot = dict(default); missing_slot["rule4"] = default["rule4"].replace("{locale_label}", "모국어")
    assert any("슬롯" in p and "누락" in p for p in el.validate("expression", missing_slot, default))

    unknown_slot = dict(default); unknown_slot["rule1"] = default["rule1"] + " {level}"
    assert any("미지" in p for p in el.validate("expression", unknown_slot, default))

    stray_brace = dict(default); stray_brace["rule1"] = default["rule1"] + " {"
    assert any("중괄호" in p for p in el.validate("expression", stray_brace, default))

    banned = dict(default); banned["rule1"] = default["rule1"] + " 마무리하자."
    assert any("금지어 «마무리»" in p for p in el.validate("expression", banned, default))

    quiz = dict(default); quiz["rule1"] = default["rule1"] + " 퀴즈를 내라."
    assert any("금지어 «퀴즈»" in p for p in el.validate("expression", quiz, default))

    bracket = dict(default); bracket["rule4"] = default["rule4"] + "\n[새 블록] 이런 거"
    assert any("대괄호" in p for p in el.validate("expression", bracket, default))

    existing_bracket = dict(default); existing_bracket["persona_intro"] = default["persona_intro"].replace("[모국어] 학습자의", "[모국어] 이 학습자의")
    assert el.validate("expression", existing_bracket, default) == [], "기본판에 있던 라벨 줄은 고쳐도 된다"

    fat = dict(default); fat["rule1"] = default["rule1"] + " 가" * 600
    assert any("토큰 예산" in p for p in el.validate("expression", fat, default))

    dropped = dict(default); del dropped["rule4"]
    assert any("섹션 집합" in p for p in el.validate("expression", dropped, default))


# --------------------------------------------------------------------------- #
# 폴백 — 깨진 편집 파일은 버리고 기본판(ERROR 로그), 기동은 한다
# --------------------------------------------------------------------------- #
def test_broken_editable_falls_back_to_default_with_error_log(tmp_path, monkeypatch, caplog) -> None:
    src = os.path.join(el.EDITABLE_DIR, "expression.default.md")
    with open(src, encoding="utf-8") as f:
        default_text = f.read()
    (tmp_path / "expression.default.md").write_text(default_text, encoding="utf-8")
    broken = default_text.replace("{locale_label}로 짚고", "모국어로 짚고", 1) + "\n## extra\n마무리 정리 마지막\n"
    (tmp_path / "expression.md").write_text(broken, encoding="utf-8")
    monkeypatch.setattr(el, "EDITABLE_DIR", str(tmp_path))
    el.reset_cache()
    with caplog.at_level(logging.ERROR, logger=el.__name__):
        sections = el.load("expression")
    assert sections == el.parse(default_text), "기본판으로 폴백"
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("거절" in m for m in msgs) and any("폴백" in m for m in msgs)
    assert el.section("expression", "rule4") == el.parse(default_text)["rule4"]


def test_missing_editable_file_falls_back_to_default(tmp_path, monkeypatch, caplog) -> None:
    src = os.path.join(el.EDITABLE_DIR, "leveltest.default.md")
    with open(src, encoding="utf-8") as f:
        default_text = f.read()
    (tmp_path / "leveltest.default.md").write_text(default_text, encoding="utf-8")
    monkeypatch.setattr(el, "EDITABLE_DIR", str(tmp_path))
    el.reset_cache()
    with caplog.at_level(logging.ERROR, logger=el.__name__):
        assert el.load("leveltest") == el.parse(default_text)
    assert any("없음" in r.getMessage() for r in caplog.records)


def test_a_legit_tone_edit_passes_and_reaches_the_assembled_prompt(tmp_path, monkeypatch) -> None:
    """정상 편집(톤 문장 한 절 교체)은 통과하고 조립 결과에 그대로 실린다 — 슬롯·금지어·라벨을 안 건드린 편집."""
    import importlib
    src = os.path.join(el.EDITABLE_DIR, "expression.default.md")
    with open(src, encoding="utf-8") as f:
        default_text = f.read()
    (tmp_path / "expression.default.md").write_text(default_text, encoding="utf-8")
    edited = default_text.replace("교정하는 순간에도 톤을 순화하지 말고 통화 끝까지 처음 강도를 유지하라.",
                                  "교정하는 순간에도 톤을 순화하지 말고 통화 끝까지 처음 강도를 그대로 밀고 가라.", 1)
    assert edited != default_text
    (tmp_path / "expression.md").write_text(edited, encoding="utf-8")
    monkeypatch.setattr(el, "EDITABLE_DIR", str(tmp_path))
    el.reset_cache()
    assert "그대로 밀고 가라" in el.section("expression", "persona_intro")
    import core.prompts.expression as ex
    importlib.reload(ex)
    try:
        out = ex.build_expression_instruction(role="r", personality="p", level_profile="l", locale="en", interests=[], name="T",
                                              items=[{"obj": "물", "des": "water", "ex": None}], quiz_group=3)
        assert "그대로 밀고 가라" in out and "[퀴즈]" in out
    finally:
        monkeypatch.undo()          # ⚠ 원래 EDITABLE_DIR 로 되돌린 **뒤** 다시 로드 — 안 그러면 편집본이 모듈에 남아 다른 시험(기준 해시)이 터진다
        el.reset_cache()
        importlib.reload(ex)
