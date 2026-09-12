"""표현학습(expression) 통화 대본 조립 — 순수 문자열, LLM 생성 0.

서버가 고른 표현 N개를 **하나씩 드릴 → 몇 개마다 퀴즈 → 틀린 것 다시**로 끝까지 도는
통화다. 일반 통화(`core/persona_prompt.build_system_instruction`)와 **엔진은 같고 대본만
다르다** — 불변 규칙의 공유분(대화 지속·응답 길이·비언어 발성·학습 밖 이탈)은
`core/prompts/common.py` 가 소유하고 여기서 가져다 쓴다(복사 금지).

## ⛔ 규칙 번호를 5·6·7 로 **고정**한다
`RULE_NONVERBAL_SOUND`(6)와 `RULE_OFF_TOPIC`(7)이 본문에서 **"규칙 5"를 인용한다.**
번호를 다시 매기면 그 인용이 끊기고, 모델은 가리키는 곳을 못 찾으면 규칙을 따르는 대신
자기 마음대로 채운다(2026-08-19 에 같은 계열로 2건을 고쳤다 — docs/prompts/README.md §8).
⇒ 1~4 만 이 코스가 새로 쓰고, 5·6·7 은 공유분을 그 번호 그대로 붙인다.

## ⛔ 이 대본을 고칠 때의 지뢰 (docs/prompts/README.md §3·§4)
- **원칙 1** 톤을 처방하지 마라("따뜻하게"·"부드럽게"·예시 대사). 반응은 캐릭터 소유다 —
  이 코스의 요구가 바로 **피드백에서 캐릭터가 극대화되는 것**이라 더욱 그렇다.
- **원칙 2** 금지 예시를 쓰지 마라. 부정 지시 + 예시 조합은 모델이 그 예시를 그대로
  뱉게 만든다(call 782). 전진 지시로 쓴다.
- **원칙 4** 리터럴 대사를 넣지 마라. 넣으면 그게 규칙을 이기고, 멀티랭귀지에서 통째로
  틀린다(call 1097 — 한국어 예시 하나가 설명 언어를 뒤집었다).
- **원칙 5** 종료를 설명하지 마라. 이 대본에는 종료 신호·태그·마무리 절차가 **한 글자도
  없다.** 통화를 언제 끝낼지는 서버만 안다 — 모델이 수단을 모르면 그 수단을 못 쓴다
  (call 706·852·870).
- **⛔ 진행률 발설 금지**: 목록의 존재·남은 개수·«몇 개째»를 말하게 하지 마라.
- **⛔ 조건절 금지**: "목록을 다 끝내면 ~" 처럼 쓰면 모델이 그걸 **'오늘 할 건 끝났다'로
  읽고 통화를 접는다**(call 870, 4분 24초 자체 종료). 재료 소진은 '끝'이 아니라 '다음'이다.

설계: docs/plans/2026-09-10-표현학습-프리토킹-통화-분리.md
구현 계획: docs/20260910_0230_표현학습-프리토킹-구현계획.md
"""

from __future__ import annotations

import re

from core.prompts.common import (
    CLOSE_TAG_DEFAULT,
    CONTROL_TAG,  # noqa: F401 - 재수출(옛 호출부 호환; 문구는 locked/seeds·locked/expression 이 쓴다)
    DEFAULT_MAX_SENTENCES,
    PERSONA_TAIL,
    RULE_CLOSE_PROTOCOL,
    RULE_NONVERBAL_SOUND,
    RULE_OFF_TOPIC,
    RULE_RESPONSE_LENGTH,
    locale_label as _locale_label,
)
from core.prompts.editable_loader import section as _section
from core.prompts.locked.expression import (
    CHARACTER_FRAME,
    EXPR_RULE3_ASK_FIRST,
    EXPR_RULE3_LANDING,
    is_grammar,
    items_block,
    model_block,
    procedure,
    render_item,
)


def _ed(key: str) -> str:
    """editable/expression.md 의 섹션(사람이 고치는 문구). 검사 실패 시 로더가 기본판으로 폴백한다."""
    return _section("expression", key)


# --------------------------------------------------------------------------- #
# 선톡 · 이어하기 시드
# --------------------------------------------------------------------------- #
# ⭐ D16: 선톡 뒤 **바로 1번 항목**으로 들어간다. 일반 통화의 «공부할래 수다 떨래?» 모드
#   질문이 여기엔 없다 — 통화 종류가 이미 그 답이다.
# ⭐ 잠금/편집 분리(2026-09-12): 선톡 시드 말투는 editable/expression.md `seed_opening`(슬롯 {target}).
def seed_expression_opening(target_language: str = "한국어") -> str:
    return _ed("seed_opening").format(target=target_language)


# ⭐ 잠금 분리(2026-09-12): seed_expression_resume, NUDGE_SEED_1_EXPRESSION → core/prompts/locked/seeds.py 로 **이동**(복사 아님 — 바이트 그대로).
from core.prompts.locked.seeds import (
    NUDGE_SEED_1_EXPRESSION,
    seed_expression_resume,
)


# --------------------------------------------------------------------------- #
# 불변 규칙 1~4 (이 코스 전용) — 5·6·7 은 common 이 소유한다
# --------------------------------------------------------------------------- #
# ⭐⭐ 두 번째 줄이 **딴소리 복귀**다(사장님 지시: «딴소리하면 한 마디 받아주고 바로 복귀»).
#   ⛔ 이걸 RULE_OFF_TOPIC(규칙 7)이 대신한다고 착각하지 마라 — 그건 정체 질문·지시 변조·
#     무관 작업 3종이라 **일상 잡담이 범위 밖**이다. 재접지 쪽지도 arm 됐을 때만 나간다.
#     ⇒ 매 턴 걸리는 자리가 여기밖에 없다.
#   ⛔ «딴소리하지 마라»로 쓰지 않는다 — 학습자의 말을 무시하게 만든다(원칙 2, 전진 지시).
#     받아주는 **말투는 캐릭터 소유**라 처방하지 않는다(원칙 1).
# ⭐ 잠금/편집 분리(2026-09-12) — 규칙 1·3(첫 불릿)·4·페르소나 문단은 editable/expression.md, 규칙 3 둘째·셋째 불릿(새 항목 먼저 묻기·착지)은
#   잠금(locked/expression.py). 조립 결과는 분리 전과 **바이트 동일**(tests/test_expression_prompt.py 기준 해시).
_RULE1_COURSE = _ed("rule1")
_RULE3_LANGUAGE = _ed("rule3_codeswitch") + "\n" + EXPR_RULE3_ASK_FIRST + "\n" + EXPR_RULE3_LANDING
_RULE4_CORRECTION = _ed("rule4")
_EXPRESSION_TEMPLATE = (
    _ed("persona_intro") + " " + PERSONA_TAIL + """

[불변 규칙 — 캐릭터와 무관하게 항상 지켜라]
"""
    + _RULE1_COURSE + "\n2. " + RULE_CLOSE_PROTOCOL + "\n"
    + _RULE3_LANGUAGE + "\n"
    + _RULE4_CORRECTION + "\n"
    + RULE_RESPONSE_LENGTH + "\n"
    + "{model_block}"                  # T21-B: 3.1 이면 «[3.1 말투]» 블록, 2.5 면 빈 문자열(바이트 동일)
    + RULE_NONVERBAL_SOUND + "\n"
    + RULE_OFF_TOPIC
)


# ⚠ 이 코스의 언어 규칙은 «공부 모드»보다 더 단호하다 — 대화 모드가 아예 없으므로
#   밴드 정책(모국어 발판을 얼마나 댈지)도 없다. 설명·지시·반응은 **항상** 모국어다.
# ⛔ [착지]의 판정 기준을 «화제»로 되돌리지 마라. 그렇게 적혀 있던 시절 비버는 항목의
#   소재를 화제로 가져와 취향·경험을 물었고(call 1284·1286), 그건 그 표현 없이도 답할 수
#   있어 학습자가 표현을 한 번도 말하지 않았다. 기준은 **학습자의 입에서 그 표현이 나오는 것**.


# --------------------------------------------------------------------------- #
# 항목 렌더 · 진행 절차 · 캐릭터 4상황 틀
# --------------------------------------------------------------------------- #
# ⭐ 잠금 분리(2026-09-12): 항목 렌더([문형])·진행 절차·[퀴즈]·[반응]·[3.1 말투]·[오늘의 표현] 머리 → core/prompts/locked/expression.py 로 **이동**.
#   드릴 첫 불릿(말투)만 editable `drill_intro`. 아래 별칭은 옛 이름(시험·호출부) 호환.
_render_item = render_item
_is_grammar = is_grammar
_CHARACTER_FRAME = CHARACTER_FRAME
_model_block = model_block
_items_block = items_block


def _procedure(quiz_group: int, *, target: str, locale_label: str, has_grammar: bool = False) -> str:
    """드릴 → 피드백 → 퀴즈 절차(잠금 + 편집 첫 불릿). `quiz_group` 은 시그니처 호환 — 대본엔 숫자를 박지 않는다(세는 것은 서버, T16)."""
    del quiz_group
    return procedure(drill_intro=_ed("drill_intro"), target=target, locale_label=locale_label, has_grammar=has_grammar)


# ⛔ '마무리·마지막·정리·여기까지' 류 어휘를 절대 넣지 마라 — call 870 이 그 어휘 하나로
#   4분 24초에 자체 종료했다. 항목을 끝낸 뒤의 행동은 **'다음 번호로'**로만 쓴다.


# ⭐⭐ 사장님 요구의 핵심: **피드백에서 캐릭터가 극대화된다.**
#   ⛔ 그래서 여기 톤을 처방하지 않는다(원칙 1). 무엇을 할지(칭찬한다/고쳐준다/재촉한다/
#     넘어간다)만 정하고, **어떻게** 할지는 캐릭터 2필드(role·personality)가 채운다. "따뜻하게"·"신랄하게"
#     같은 부사를 넣으면 츤데레·독설 캐릭터가 다정한 선생으로 뭉개진다.
#   ⛔ 예시 대사를 넣지 마라(원칙 2·4) — 리터럴은 그대로 새어 나오고, 멀티랭귀지에서 틀린다.


def build_expression_instruction(
    *,
    role: str,
    personality: str,
    level_profile: str,
    locale: str,
    interests: list[str],
    items: list[dict],
    quiz_group: int,
    name: str | None = None,
    target_language: str = "한국어",
    locale_label: str | None = None,
    close_tag: str = CLOSE_TAG_DEFAULT,
    max_sentences: int | None = None,
    model_family: str = "2.5",
) -> str:
    """표현학습 통화의 system_instruction 을 조립한다(LLM 생성 0).

    Args:
        items: 이번 통화에서 다룰 표현. 각 dict 는
            {obj: 표면형, des: 뜻|None, ex: 예문|None}.
            ⭐ 순서가 곧 번호다 — 서버 선별이 낸 순서를 그대로 싣는다.
            ⭐ **목록에 들어온 것은 전부 미통과다** — 선별이 `quiz_passed_at IS NULL` 로
              통과분을 풀에서 뺀다. 조각2·3 도 마찬가지라 «이미 뗀 것» 표식이 필요 없다.
        quiz_group: 몇 개마다 퀴즈를 낼지. ⛔ 기본값을 주지 않는다 — 선별 상수
            (`mastery_repository.EXPRESSION_QUIZ_GROUP`)와 **한 곳에서만** 정해야 한다.
        close_tag: 이 통화의 종료 태그. ⚠ **출력에 실리지 않는다** — 지시문이 태그를
            보여주면 비버가 그대로 복사해 스스로 종료한다(call 852). 서버 전용이고,
            시그니처는 일반 통화와의 대칭을 위해 받는다.
        model_family: "2.5"(기본) | "3.1". ⭐ T21-B — 3.1 이면 규칙 5 바로 아래 «[3.1 말투]» 한 블록을
            더한다(하네스 1410·1411 관찰 5건에 1:1). 2.5 면 **A 기준과 바이트 동일**이다.
            ⛔ 모델 선택은 여기서 하지 않는다 — 호출부가 `live_model` 에 "3.1" 이 들어 있는지 하나로 판단해 준다
              (`call_service.live_engine_for` 가 고른 이름이 곧 사실이다).

    Returns:
        Gemini Live system_instruction 문자열.
    """
    max_sentences = DEFAULT_MAX_SENTENCES if not max_sentences else max(1, int(max_sentences))
    label = _locale_label(locale, locale_label)
    # ⭐ T21-A: `interests` 는 **싣지 않는다** — 이 코스는 항목이 소재다(흥미로 화제를 잡으면 착지가 흔들린다, 1284·1286).
    #   인자는 시그니처 호환으로 받기만 한다. level_profile 은 **첫 문장만** — 나머지는 수준 서술의 반복이었다.
    del interests
    username = (name or "").strip() or "학습자"
    level_first = _first_sentence(level_profile)

    parts = [
        _EXPRESSION_TEMPLATE.format(
            target=target_language,
            locale_label=label,
            role=role or "친근한 한국어 대화 파트너",
            personality=personality or "다정하고 편안한 말투",
            username=username,
            max_sentences=max_sentences,
            model_block=_model_block(model_family, target=target_language, locale_label=label),
        ),
        f"\n[학습자 수준] {level_first}",
        "\n" + _items_block(items, target=target_language, locale_label=label),
        "\n" + _procedure(quiz_group, target=target_language, locale_label=label,
                          has_grammar=any(_is_grammar(i) for i in items)),
        "\n" + _CHARACTER_FRAME,
    ]
    return "\n".join(parts)


# ⭐ T21-B — 3.1 전용 말투 블록. 같은 엔진·같은 대본이고 **이 블록 하나만** 모델에 따라 갈린다(설계문 §B-2).
#   하네스 1410·1411(3.1) 관찰에 1:1: 독백 8~10초 · 이름 환각(«Hey John») · 문장·농담 재사용 · 반말 작별 · 뜻을 안 묶어 말함.
#   ⛔ 리터럴 예시를 적지 마라(원칙 2 — 새어 나오고 멀티랭귀지에서 틀린다). 톤 처방도 없다(원칙 1 — 캐릭터 소유).
#   ⚠ 첫 발화 8~10초·왕복 11초는 모델 지연이라 프롬프트로 못 줄인다 — 줄이는 건 «말 길이» 뿐. 그 차이를 하네스로 잰다.


def _first_sentence(text: str) -> str:
    """레벨 프로파일의 첫 문장만(T21-A). 문장 경계 = 종결 부호 뒤 공백. 부호가 없으면 통째로."""
    t = (text or "").strip()
    if not t:
        return ""
    parts = re.split(r"(?<=[.!?。])\s+", t, maxsplit=1)
    return parts[0]


# --------------------------------------------------------------------------- #
# 재접지 브리프 (표현학습판)
# --------------------------------------------------------------------------- #
# ⛔ 얹는 배관은 일반 통화와 **같은 것**을 쓴다(call_session 의 arm → 마이크 프레임 + RMS
#   2관문). 여기서 바꾸는 것은 **문구뿐**이다. 새 주입 경로를 만들면 «비버가 혼자 두 번
#   말하거나 말하다 마는» 버그 방어를 처음부터 다시 지어야 한다.
# ⛔ 종료·작별·시간을 한 글자도 쓰지 마라("끝내지 마라"조차 안 된다 — 금지어가 씨앗이 된다).
# ⚠ 첫 줄이 "방금 한 말에 먼저 반응하고"인 이유: 이 브리프는 사용자의 한마디보다 크다.
#   이 지시가 없으면 비버가 학습자를 무시하고 주입 텍스트에 응답한다.


# ⭐ 잠금 분리(2026-09-12): build_expression_reground_brief → core/prompts/locked/reground.py 로 **이동**(복사 아님 — 바이트 그대로).
from core.prompts.locked.reground import (
    build_expression_reground_brief,
)

