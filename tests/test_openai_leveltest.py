"""레벨테스트 대본 — ⭐⭐ **두 엔진이 같은 글자**임을 지킨다(사장님 지시 2026-10-09).

네 코스 중 레벨테스트만 Gemini 정본과 같은 대본을 쓴다. 그런데 GPT 쪽은 그 함수를
**호출하지 않고 베껴 적었다**(`core/openai/prompts/leveltest.py` — 이 패키지는
`core.persona_prompt` 를 import 할 수 없다. 격리 규칙이 AST 로 막는다).

⚠⚠ **그래서 두 곳이 조용히 갈라질 수 있다.** 제미나이 쪽은
`core/prompts/{locked,editable}/leveltest.md` 에서 조립되므로, 그 md 를 고치면 GPT 쪽은
따라오지 않는다. 이 파일이 그 갈라짐을 **글자로** 잡는 유일한 장치다.

⛔ 이 시험이 깨졌다면 선택지는 둘뿐이다:
  ① md 를 고쳤다 → `core/openai/prompts/leveltest.py` 의 상수에 **같은 수정**을 옮긴다.
  ② 일부러 갈랐다 → 사장님 확인을 받고, 이 시험을 「갈라졌다」는 사실을 적는 시험으로 바꾼다.
"""
from __future__ import annotations

import pytest

# 두 엔진에 **같은 인자**를 넣는다. 호출부(`call_session.py`)가 엔진 분기만으로
# 갈아타려면 시그니처가 같아야 하므로, 같은 dict 를 그대로 둘에 흘린다.
_KW = dict(
    role="비버 선생님", personality="장난기 있고 직설적",
    locale="en", interests=["여행", "요리"], name="재우",
)

# ⭐ 마지막은 **미등록 언어**다 — 사다리가 없을 때 둘 다 한국어로 떨어지는지 본다
#   (폴백이 갈리면 그 언어 사용자만 다른 대본을 받는다. 가장 늦게 발견되는 종류의 사고다).
_LANGS = ["한국어", "일본어", "영어", "중국어", "프랑스어", "베트남어", "스와힐리어"]


def _gem(**kw) -> str:
    from core.persona_prompt import build_leveltest_instruction
    return build_leveltest_instruction(**{**_KW, **kw})


def _gpt(**kw) -> str:
    from core.openai.prompts.leveltest import build_leveltest_instruction
    return build_leveltest_instruction(**{**_KW, **kw})


@pytest.mark.parametrize("target", _LANGS)
def test_leveltest_instruction_is_identical_across_engines(target: str):
    """⛔ 지시문이 **글자까지** 같아야 한다 — 언어별 사다리와 폴백까지."""
    gem, gpt = _gem(target_language=target), _gpt(target_language=target)
    assert gpt == gem, (
        "레벨테스트 대본이 두 엔진에서 갈라졌다(target=%s)\n"
        "— core/prompts/*/leveltest.md 를 고쳤으면 core/openai/prompts/leveltest.py 에도 옮겨라."
        % target
    )


@pytest.mark.parametrize("target", ["한국어", "영어", "스와힐리어"])
def test_leveltest_opening_seed_is_identical_across_engines(target: str):
    """⛔ 선톡 시드도 같은 글자다 — 0단(인사·정형표현)부터 재라는 자리다."""
    from core.openai.prompts.leveltest import seed_leveltest_opening as gpt_seed
    from core.persona_prompt import seed_leveltest_opening as gem_seed

    assert gpt_seed(target) == gem_seed(target), "선톡 시드가 갈라졌다(target=%s)" % target


def test_leveltest_locale_label_table_matches():
    """⛔ 로케일 라벨표를 베껴 적었다 — 빠진 로케일이 있으면 그 사용자만 영어로 떨어진다."""
    from core.openai.prompts import leveltest as gpt
    from core import persona_prompt as gem

    assert gpt._LOCALE_LABEL == gem._LOCALE_LABEL, "로케일 라벨표가 갈라졌다"
    assert gpt._DEFAULT_LOCALE == gem._DEFAULT_LOCALE


def test_leveltest_locale_label_override_is_honoured():
    """`locale_label` 오버라이드가 둘 다 같게 먹는다(호출부가 모르는 로케일을 넘길 때의 길)."""
    assert _gpt(target_language="한국어", locale_label="스와힐리어(Kiswahili)") == \
        _gem(target_language="한국어", locale_label="스와힐리어(Kiswahili)")


def test_leveltest_keeps_the_close_vocabulary_on_purpose():
    """⭐ 다른 세 대본과 **반대로** 종료 어휘가 있어야 한다.

    표현학습·회화학습·자유대화에선 종료 어휘가 금지다(call 706·852·870 — 종료 개념을
    가르치면 모델이 스스로 끊었다). 레벨테스트는 **반대 방향 설계**다: 「끝내는 건
    서버다」를 명시로 눌러야 측정이 끝까지 간다. 정본이 그렇게 쓰여 있고 사장님이
    동일하게 쓰라고 하셨다 ⇒ 이 글자가 사라지면 그건 교체 사고다.
    """
    text = _gpt(target_language="한국어")
    assert "통화를 끝내는 건 서버다" in text
    assert "스스로 끝내지 말고" in text


def test_leveltest_module_does_not_import_gemini_assets():
    """⛔ 베껴 적은 이유가 이것이다 — import 하면 격리가 깨진다(전수 검사는 isolation 쪽)."""
    import pathlib
    src = pathlib.Path("core/openai/prompts/leveltest.py").read_text(encoding="utf-8")
    for banned in ("core.persona_prompt", "core.prompts"):
        # 독스트링에서 «출처» 로 언급하는 것은 괜찮다 — `import` 문만 본다.
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert banned not in stripped, line
