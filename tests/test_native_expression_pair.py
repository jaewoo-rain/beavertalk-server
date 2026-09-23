"""C8(2026-09-23) — 현지인 표현 추출(같은 분석 호출). 순수 스키마·지시문 시험(DB·LLM 없음).

시험 목록(문서 그대로 + bt-back 경계조건):
    - 스키마 파싱(칸 있음/없음)
    - 지시문에 금지 규칙 포함(심한 욕·혐오·성적 금지 / 과장·줄임말·가벼운 비속어 허용)
    - 원문과 같으면(또는 짝이 없으면) None 으로 비움(빈 문자열 금지)
    - 학습 대상 언어 기준(모국어로 짝을 만들지 않는다는 지시)
근거: docs/plans/2026-09-22-프리미엄-자유대화-15분-달력.md(C8)
"""

from __future__ import annotations

from domains.learning.service.normalcall_service import (
    LearnedExpression,
    _analysis_instruction,
    _normalize_native_pair,
)


# --------------------------------------------------------------------------- #
# 1) 스키마 파싱 — 칸 있음/없음 둘 다 파싱이 안 죽는다(선택 칸, bt-back 경계조건 ⑥)
# --------------------------------------------------------------------------- #
def test_schema_parses_without_native_fields():
    """구스키마 응답(현지인 짝 칸이 아예 없음)도 파싱된다 — 하위호환."""
    e = LearnedExpression(korean="감사합니다", translation="thank you", source_type="asked")
    assert e.native_expression is None
    assert e.native_expression_translation is None
    assert e.native_nuance is None


def test_schema_parses_with_native_fields():
    e = LearnedExpression(
        korean="감사합니다", translation="thank you", source_type="asked",
        native_expression="고마워요", native_expression_translation="thanks",
        native_nuance="더 친근하고 캐주얼한 말투",
    )
    assert e.native_expression == "고마워요"
    assert e.native_expression_translation == "thanks"
    assert e.native_nuance == "더 친근하고 캐주얼한 말투"


def test_schema_tolerates_a_missing_native_nuance_alone():
    """일부 칸만 빠져도(모델이 nuance 를 깜빡함) 파싱이 죽지 않는다."""
    e = LearnedExpression(
        korean="감사합니다", translation="thank you", source_type="asked",
        native_expression="고마워요", native_expression_translation="thanks",
    )
    assert e.native_nuance is None


# --------------------------------------------------------------------------- #
# 2) 지시문 — 금지/허용 규칙이 명시적으로 들어 있다
# --------------------------------------------------------------------------- #
def test_instruction_states_the_forbidden_and_allowed_content_rules():
    instruction = _analysis_instruction("en", "한국어")
    assert "심한 욕설" in instruction
    assert "혐오" in instruction
    assert "성적" in instruction
    assert "과장" in instruction
    assert "줄임말" in instruction
    assert "가벼운 비속어" in instruction


def test_instruction_requires_the_same_meaning():
    instruction = _analysis_instruction("en", "한국어")
    assert "같은 뜻" in instruction


def test_instruction_is_target_language_based_not_native_language():
    """⭐⭐ bt-back 경계조건 ②: 학습 대상 언어(target_language) 기준이지 모국어
    (locale)로 짝을 만들라고 하면 안 된다."""
    instruction = _analysis_instruction("en", "일본어")
    assert "일본어를 쓰는 현지인" in instruction
    assert "학습자의 모국어로 짝을 만들지 마라" in instruction


def test_instruction_says_to_omit_rather_than_fill_blank():
    instruction = _analysis_instruction("en", "한국어")
    assert "전부 생략" in instruction
    assert "빈 문자열로" in instruction


# --------------------------------------------------------------------------- #
# 3) _normalize_native_pair — 짝이 없거나 원문과 같으면 None(빈 문자열 금지)
# --------------------------------------------------------------------------- #
def test_normalize_clears_all_three_when_native_equals_original():
    e = LearnedExpression(
        korean="감사합니다", translation="thank you", source_type="asked",
        native_expression="감사합니다", native_expression_translation="thank you",
        native_nuance="같은 표현",
    )
    _normalize_native_pair(e)
    assert e.native_expression is None
    assert e.native_expression_translation is None
    assert e.native_nuance is None


def test_normalize_clears_all_three_when_native_is_an_empty_string():
    """⭐⭐ bt-back 경계조건 ①: 모델이 지시를 어기고 빈 문자열을 냈어도 None 으로
    정리한다(빈 문자열이 그대로 남으면 안 된다)."""
    e = LearnedExpression(
        korean="감사합니다", translation="thank you", source_type="asked",
        native_expression="   ", native_expression_translation="thanks",
        native_nuance="뭔가",
    )
    _normalize_native_pair(e)
    assert e.native_expression is None
    assert e.native_expression_translation is None
    assert e.native_nuance is None


def test_normalize_keeps_a_genuine_pair_and_trims_whitespace():
    e = LearnedExpression(
        korean="감사합니다", translation="thank you", source_type="asked",
        native_expression="  고마워요  ", native_expression_translation="  thanks  ",
        native_nuance="  더 캐주얼함  ",
    )
    _normalize_native_pair(e)
    assert e.native_expression == "고마워요"
    assert e.native_expression_translation == "thanks"
    assert e.native_nuance == "더 캐주얼함"


def test_normalize_leaves_missing_translation_or_nuance_as_none():
    e = LearnedExpression(
        korean="감사합니다", translation="thank you", source_type="asked",
        native_expression="고마워요",
    )
    _normalize_native_pair(e)
    assert e.native_expression == "고마워요"
    assert e.native_expression_translation is None
    assert e.native_nuance is None
