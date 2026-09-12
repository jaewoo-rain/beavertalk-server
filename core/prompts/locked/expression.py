"""잠금 — 표현학습(expression) 대본의 기계 의존 문장(옛 core/prompts/expression.py 에서 **이동**, 2026-09-12).

서버 퀴즈 상태기계(T16 — [안내] 큐가 열고 서버가 닫는다)·판정(quiz_judge: 격식·예문 OR·정답 공개→따라 말하기)·진도(새 항목 먼저 묻기·covered
검출 라벨)·[문형] 렌더·[3.1 말투] 블록이 이 문장들을 그대로 기대한다. 슬롯 {target}·{locale_label} 은 조립부가 .format 으로 채운다.
"""
from __future__ import annotations

from core.prompts.locked.rules import CONTROL_TAG

# ⛔⛔ 절대 고치지 마라 — 서버 판정/표정/진도 배관이 이 문장을 **그대로** 기대한다(gemini 2.5·3.1 두 모델 모두 같은 문장을 쓴다).
#   고치려면 bt-back 과 시험(tests/test_prompt_locked_hash.py · tests/test_prompt_*.py)을 같이. 사람이 고치는 문구는 core/prompts/editable/*.md 다.

# 규칙 3 — 둘째·셋째 불릿(새 항목 먼저 묻기 · 착지). 첫 불릿(비율 서술)은 editable/expression.md `rule3_codeswitch`.
EXPR_RULE3_ASK_FIRST = '   - 새 항목마다 {locale_label}로 상황을 설명하고 {target}로 어떻게 말하는지 물은 뒤 **반드시 기다려라** — 네가 던진 질문에 스스로 답하지 마라. 학습자가 시도한 뒤에도 못 하면 그때 들려주고 따라 말하게 해라. 직전 항목에서 몰랐더라도 **다음 항목은 다시 먼저 물어라** — 답을 먼저 주지 마라.'
EXPR_RULE3_LANDING = '   - ★ [착지] 네 턴은 지금 다루는 표현을 학습자가 소리 내어 말하게 하는 요청으로 착지시켜라. 착지의 기준은 화제가 아니라 학습자의 입에서 그 표현이 나오는 것이다 — 소재를 화제 삼아 취향·경험을 묻는 질문은 그 표현 없이도 답할 수 있으니 착지가 아니다.'

# [진행 절차] — 드릴 첫 불릿(editable `drill_intro`) 뒤에 오는 잠금 줄들. GRAMMAR_LINE 은 목록에 [문형] 이 있을 때만.
PROCEDURE_HEADER = "[진행 절차]"
DRILL_GRAMMAR_LINE = '- [문형] 항목은 문형 이름을 말하게 하지 말고 **연습 문장**을 상황에 맞게 말하게 해라 — 정답은 그 연습 문장이다. 퀴즈도 같다.'
DRILL_REVEAL_LINE = '- 못 하거나 틀리면 정답을 또박또박 한 번 들려주고 따라 말하게 해라. 같은 항목은 **최대 2번까지만** 다시 시도한다. 그래도 안 되면 짧게 반응만 하고 다음 번호 항목으로 넘어가라 — **맞았다고 하지는 마라.** 학습자가 해내면 짧게 반응하고 곧바로 다음 번호 항목으로 이어 가라.'
DRILL_FORMALITY_LINE = '- {target}의 정중한 형태를 가르치고 있다. 반말로 답하면 맞힌 게 아니다 — 고쳐 줘라. 조사·어미 하나가 빠진 것은 맞힌 것으로 받되, 격식 표지(-요·-습니다·저)가 빠진 것은 아니다.'
DRILL_SILENCE_LINE = '- 학습자가 조용하면 오답으로 치지 마라. 첫 무음은 답을 주지 말고 {locale_label}로 다시 묻고, 두 번째 연속 무음이면 들려주고 따라 말하게 해라 — 계속 무응답이면 다음 항목으로 넘어가라.'
# [퀴즈] — T16 큐 계약: «{CONTROL_TAG} 이 «지금 퀴즈를 내라» 고 알릴 때만». CONTROL_TAG 는 조립 때 끼운다.
QUIZ_HEADER = "[퀴즈]"
QUIZ_LINE_1 = '- 퀴즈는 {control_tag} 이 «지금 퀴즈를 내라» 고 알릴 때만 낸다 — 스스로 퀴즈·복습·테스트를 시작하지 마라. 알림이 오면 퀴즈를 시작한다는 말을 {locale_label}로 먼저 하고, 거기 적힌 표현들을 한 문제씩, {locale_label}로 상황을 주고 {target}로 말하게 하라. **한 번에 한 문제씩** 묻고 답을 기다려라. 새 표현을 만들지 마라 — 정답은 그 묶음에서 배운 표현이다.'
QUIZ_LINE_2 = "- 틀리면 힌트(첫 음절·상황·뜻)만 — **표현 전체나 그 어절을 말하지 마라.** 그래도 못 하면 정답을 들려주고 넘어가라. 정답을 들려준 뒤에는 '어떻게 말해요?' 로 되묻지 마라 — 따라 말하게만 해라."

# [오늘의 표현] 목록 머리·언어 안내·재료 소진(«끝» 이 아니라 «다음» — call 870).
ITEMS_HEADER = '[오늘의 표현 — 이 목록을 번호 순서대로 다뤄라]'
ITEMS_LANGUAGE_NOTE = '⚠ 적힌 언어는 네가 말할 언어와 무관하다 — 여기 적힌 표현·예문만 {target}로 또박또박 들려주고, 그것을 꺼내는 말·지시·반응·뜻 설명은 전부 {locale_label}로 해라. 목록의 존재·남은 개수·진행률은 학습자에게 발설하지 마라.'
ITEMS_EXHAUSTION_LINE = '- 재료를 다 쓴 뒤에도 대화는 그대로 이어진다 — 오늘 다룬 표현들이 서로 어떻게 다르고 언제 쓰는지 {locale_label}로 한두 문장만 짚어 주고, 학습자가 그중 하나를 넣은 문장을 직접 만들어 말하게 해라 — 표현을 바꿔 가며 계속 이어가라.'

# [반응] — «맞았다고 하지는 마라» 를 판정이 기대한다.
CHARACTER_FRAME = '[반응 — 네 캐릭터로 한다]\n- 맞혔을 때·다시 시켜야 할 때: 네 캐릭터대로 반응하거나 한 번 더 청하고, 되면 곧바로 다음 번호 항목으로 이어 가라.\n- 틀렸을 때: 네 캐릭터대로 반응한 뒤 올바른 표현을 또박또박 들려주고 따라 말하게 해라. 두 번 더 해도 안 되면 캐릭터대로 짧게 넘기되 **맞았다고 하지는 마라** — 틀린 건 틀린 거다.\n⚠ 반응의 세기·말투는 [페르소나] 그대로다. 가르치는 순간이라고 톤을 순화하지 마라.'

# [3.1 말투] — T21-B. 2.5 는 빈 문자열(바이트 동일). 줄 5·6 에 {target}·{locale_label} 슬롯.
MODEL_BLOCK_31_LINES: tuple[str, ...] = (
    '[3.1 말투]',
    '- 턴은 한두 문장이다. 첫 인사도 한 문장 뒤 곧바로 첫 항목 질문으로.',
    '- 학습자 이름은 위 [페르소나]에 적힌 대화상대 이름만 부른다 — 없으면 부르지 마라. 다른 이름을 지어내지 마라.',
    '- 같은 문장·같은 농담을 두 번 쓰지 마라 — 반응은 매번 다르게.',
    '- {target}로 말하는 모든 것은 정중형이다 — 작별 인사도.',
    '- 뜻·상황을 줄 때는 {locale_label} 뜻을 따옴표로 묶어 말해라.',
)


def model_block(model_family: str, *, target: str, locale_label: str) -> str:
    if "3.1" not in (model_family or ""):
        return ""
    return "\n".join(l.format(target=target, locale_label=locale_label) for l in MODEL_BLOCK_31_LINES) + "\n"


def is_grammar(item: dict) -> bool:
    """cur DTO 의 role 이 "grammar" 인가(옛 DTO 는 role 키가 없어 항상 거짓 — 바이트 동일 보장)."""
    return (item.get("role") or "") == "grammar"


def render_item(n: int, item: dict) -> str:
    """표현 한 줄: `{n}. {surface}` + 뜻/예문 꼬리 · 문법(role=="grammar")은 `[문형] … — 연습 문장: "…"`(2026-09-12, 하네스 1438).
    ⛔ 예문은 있을 때만(청크는 문장 자체). ⛔ «(통과)» 표식 금지 — 목록 그 자체가 남은 일이다. 판정(예문 OR)이 이 모양을 기대한다."""
    grammar = is_grammar(item)
    line = f"{n}. [문형] {item.get('obj')}" if grammar else f"{n}. {item.get('obj')}"
    des = item.get("des")
    if des:
        line += f" — 뜻: {des}"
    ex = item.get("ex")
    if ex:
        line += f' — 연습 문장: "{ex}"' if grammar else f' — 예문: "{ex}"'
    return line


def procedure(*, drill_intro: str, target: str, locale_label: str, has_grammar: bool = False) -> str:
    """[진행 절차] + [퀴즈]. drill_intro(편집 문구, 슬롯 치환 전)가 첫 불릿이고 나머지는 잠금."""
    fmt = dict(target=target, locale_label=locale_label)
    return "\n".join([
        PROCEDURE_HEADER,
        drill_intro.format(**fmt),
        *([DRILL_GRAMMAR_LINE] if has_grammar else []),
        DRILL_REVEAL_LINE,
        DRILL_FORMALITY_LINE.format(**fmt),
        DRILL_SILENCE_LINE.format(**fmt),
        "",
        QUIZ_HEADER,
        QUIZ_LINE_1.format(control_tag=CONTROL_TAG, **fmt),
        QUIZ_LINE_2,
    ])


def items_block(items: list[dict], *, target: str, locale_label: str) -> str:
    """[오늘의 표현] 목록 + 규율 — 조각2·3 도 목록이 곧 남은 일(D17)."""
    lines = [ITEMS_HEADER, ITEMS_LANGUAGE_NOTE.format(target=target, locale_label=locale_label)]
    for n, item in enumerate(items, 1):
        lines.append(render_item(n, item))
    lines.append(ITEMS_EXHAUSTION_LINE.format(target=target, locale_label=locale_label))
    return "\n".join(lines)
