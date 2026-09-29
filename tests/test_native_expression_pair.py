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


# --------------------------------------------------------------------------- #
# 2-b) C9(2026-09-30) — 현지인 표현 짝이 거의 안 나오던 버그.
#
# 운영 실측(bt-back): sentence 2,926건 중 kind=native 2건뿐(09-26 이후 0건).
# 원인은 지시문의 탈출구 "자연스러운 현지인 짝이 없거나 korean 과 사실상
# 같으면 전부 생략해라" — 모델이 거의 항상 이 조건을 근거로 짝 내기를 건너
# 뛰었다(call=1716 실측: 표현 3개 전부 native_expression=None).
#
# ⛔⛔ 정정 2(2026-09-30, 사장님 지시) — "생략" 개념 자체를 지시문에서 완전히
# 뺐다(처음엔 "korean 과 글자까지 같을 때만 생략" 예외를 남겼는데, 그것도
# 조건문이라 제거). 지시문은 "모든 표현에 짝을 반드시 하나 낸다"만 말하고,
# 원문을 복사해 내는 퇴화 사례를 거르는 일은 서버(_normalize_native_pair,
# 아래 3번 섹션)에 전담시킨다 — 모델에게 "낼지 말지"를 판단하게 하는 문구
# 자체가 이 버그의 본체였다(원칙: 프롬프트는 「항상 내라」만, 걸러내기는 서버).
# --------------------------------------------------------------------------- #
def test_instruction_has_no_omission_wording_at_all():
    """⛔⛔ 되돌림 방지(정정 2) — 생략·비움·조건부 예외를 가리키는 문구가 단
    하나도 있으면 안 된다. 모델의 "낼지 말지" 판단 여지 자체가 버그의 본체였다."""
    instruction = _analysis_instruction("en", "한국어")
    native_block = instruction[instruction.index("[현지인 표현 짝]"):]
    for banned in ("생략", "비워", "비운다", "없으면"):
        assert banned not in native_block, f"'{banned}' 가 여전히 있다(조건부 예외 재발)"


def test_instruction_defaults_to_always_producing_a_pair():
    instruction = _analysis_instruction("en", "한국어")
    assert "반드시 하나씩" in instruction


def test_instruction_has_a_worked_example():
    """⛔⛔ 정정(2026-09-30, bt-back) — 원 요청(S1)의 예시는 **입말·관용구**지
    반말 변환이 아니다. 처음엔 "제 잘못이 아닙니다→내 탓 아니야"(격식→반말)를
    썼는데, 이게 모델을 "반말 변환기"로 끌고 갔다(실험: "저는 미국 사람이에요
    →나 미국 사람이야" 류만 나옴 — 뜻은 같지만 원 요청과 축이 다르다). S1 원문
    예시("배고파요→뱃가죽이 등에 붙을 것 같아요")로 교체한다."""
    instruction = _analysis_instruction("en", "한국어")
    assert "배고파요" in instruction
    assert "뱃가죽이 등에 붙을 것 같아요" in instruction


def test_instruction_targets_vividness_not_formality():
    """⭐⭐ 정정(2026-09-30) — 지시문이 "격식/반말을 바꾸는 게 목적이 아니다"를
    명시해야 한다. 이 문구가 없으면 모델이 존댓말→반말 변환만 반복한다(실험으로
    확인)."""
    instruction = _analysis_instruction("en", "한국어")
    assert "격식" in instruction and "목적이 아니다" in instruction
    assert "관용구" in instruction


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
