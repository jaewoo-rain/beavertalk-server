"""잠금 — 표현학습(expression) 대본의 기계 의존 문장(옛 core/prompts/expression.py 에서 **이동**, 2026-09-12).

서버 퀴즈 상태기계(T16 — [안내] 큐가 열고 서버가 닫는다)·판정(quiz_judge: 격식·예문 OR·정답 공개→따라 말하기)·진도(새 항목 먼저 묻기·covered
검출 라벨)·[문형] 렌더·[3.1 말투] 블록이 이 문장들을 그대로 기대한다. 슬롯 {target}·{locale_label} 은 조립부가 .format 으로 채운다.
"""
from __future__ import annotations

from core.prompts.locked.rules import CONTROL_TAG

# ⛔⛔ 절대 고치지 마라 — 서버 판정/표정/진도 배관이 이 문장을 **그대로** 기대한다(gemini 2.5·3.1 두 모델 모두 같은 문장을 쓴다).
#   고치려면 bt-back 과 시험(tests/test_prompt_locked_hash.py · tests/test_prompt_*.py)을 같이. 사람이 고치는 문구는 core/prompts/editable/*.md 다.

# 규칙 3 — 둘째·셋째 불릿(새 항목 먼저 묻기 · 착지). 첫 불릿(비율 서술)은 editable/expression.md `rule3_codeswitch`.
EXPR_RULE3_ASK_FIRST = '   - 새 항목은 뜻·쓰임·원문·예문을 먼저 알려주고 따라 말하게 한 뒤 기다린다.'
EXPR_RULE3_LANDING = '   - ★ [착지] 네 턴은 지금 다루는 표현을 학습자가 소리 내어 말하게 하는 요청으로 착지시켜라. 착지의 기준은 화제가 아니라 학습자의 입에서 그 표현이 나오는 것이다 — 소재를 화제 삼아 취향·경험을 묻는 질문은 그 표현 없이도 답할 수 있으니 착지가 아니다.'

# [진행 절차] — 드릴 첫 불릿(editable `drill_intro`) 뒤에 오는 잠금 줄들. GRAMMAR_LINE 은 목록에 [문형] 이 있을 때만.
PROCEDURE_HEADER = "[진행 절차]"
DRILL_GRAMMAR_LINE = '- [문형] 항목은 문형 이름을 말하게 하지 말고 **연습 문장**을 상황에 맞게 말하게 해라 — 정답은 그 연습 문장이다. 퀴즈도 같다.'
# ⭐ E(2026-09-14, 사장님 확정 — 실통화 1592 t11·t14 «not even close»): 문형 항목은 같은 문형으로 만든 다른 올바른 문장도 정답. ko·ja 공통(has_grammar 일 때만, DRILL_GRAMMAR_LINE 바로 뒤).
DRILL_GRAMMAR_ALT_LINE = '- [문형] 항목은 **같은 문형으로 만든 다른 올바른 문장**도 정답이다 — 네가 낸 연습 문장과 달라도 고치지 말고 맞았다고 해라. 문형 자체가 틀렸을 때만 교정한다.'
DRILL_REVEAL_LINE = '- 틀리면 현재 정답 문장을 다시 들려주되 원문·현지인 문장 각각 최대 2회 시도 후 다음 항목으로 넘어간다. 원문 정답은 현지인 문장 연습으로 이어지고, 현지인 정답 뒤에만 다음 항목으로 간다. 소진은 정답·퀴즈 통과로 판정하지 않는다.'
DRILL_FORMALITY_LINE = '- {target}의 정중한 형태를 가르치고 있다. 반말로 답하면 맞힌 게 아니다 — 고쳐 줘라. 조사·어미 하나가 빠진 것은 맞힌 것으로 받되, 격식 표지(-요·-습니다·저)가 빠진 것은 아니다.'
# 실통화 1550(2026-09-12): 비버가 «고맙습니다 → too formal, wrong» 4회. 서버 판정(표면형 → passed)은 무변경 — 이 줄은 비버의 «틀렸다» 반응만 막는다.
# 실통화 1636(2026-09-16): 어휘 교체만 막아선 모자랐다 — «화장실은 어디 있나요?»(t30)·«이거 하나 주세요»(t8) 를 둘 다 틀렸다고 했다.
#   둘 다 올바른 정중형이고 뜻이 같다. 그래서 «다른 표현» 의 범위를 **어미·조사·군말**까지 넓힌다(무엇이 같아야 하는지는 «뜻 + 정중함»).
DRILL_ALT_CORRECT_LINE = '- 학습자가 목표 표현 대신 **뜻이 통하는 다른 올바른 정중한 표현**을 말하면 틀렸다고 하지 마라. 어휘가 다르거나(«고마워요» 자리에 «감사합니다»), 조사·어미가 다르거나(«화장실이 어디예요?» 자리에 «화장실은 어디 있나요?»), 군말이 더 붙어도(«이거 주세요» 자리에 «이거 하나 주세요») 전부 맞은 것이다 — 맞다고 인정한 뒤 오늘 배우는 표현으로도 한 번 말해 보게 해라. 반말은 여전히 맞힌 게 아니다.'
# 실통화 1550 t72·t119·t143: «잘 지냈어요? 말해 봐. 그리고 헤어질 때는?» — 한 턴에 요청 둘. 규칙 5(길이)와 다른 축(개수)이라 절차에만 한 줄.
DRILL_ONE_ASK_LINE = '- 한 턴에 질문·요청은 **하나**만 — 두 개를 이어 묻지 마라.'
# 언어별 격식 줄(2026-09-13 ja 배선). ko 는 위 DRILL_FORMALITY_LINE **그 객체**(바이트 동일 — 해시 시험). 새 언어는 여기 한 줄.
DRILL_FORMALITY_LINE_BY_LANGUAGE: dict[str, str] = {
    "ko": DRILL_FORMALITY_LINE,
    "ja": '- {target}의 정중한 형태를 가르치고 있다. 반말(です·ます 가 없는 보통형)로 답하면 맞힌 게 아니다 — 고쳐 줘라. 조사 하나가 빠진 것은 맞힌 것으로 받되, 격식 표지(です·ます)가 빠진 것은 아니다.',
}
# ⭐ 비ko 목표어 전용 드릴 줄(2026-09-14 C1·C2, 실통화 1601 ja): ko 대본은 바이트 불변(빈 튜플) — 그 밖 언어(ja …)에만 drill_intro 바로 뒤에 들어간다.
#   C1 1601 비버가 「こんにちは」 를 '곤니치와' 로 15번 적었다 — 목표어는 목표어 문자로. C2 1601 t3·t5 처음부터 정답을 들려주고 따라 하게 했다 — 먼저 묻고,
#   시도 뒤에만 공개(DRILL_REVEAL_LINE 의 «최대 3번» 과 같은 규율을 순서로 강조). ko 는 기존 drill_intro(편집 문구)+DRILL_REVEAL_LINE 그대로.
DRILL_TARGET_SCRIPT_LINE = '- {target} 낱말·문장은 언제나 {target} 문자로 말하고 적어라 — 학습자 모국어 문자로 음차해 적거나 읽지 마라.'
DRILL_ASK_FIRST_LINE = '- 새 항목은 먼저 알려주고 원문 복창을 요청한다. 원문 정답 뒤 현지인 표현을 하나 알려주고 복창을 기다린다.'
DRILL_EXTRA_LINES_BY_LANGUAGE: dict[str, tuple[str, ...]] = {
    "ko": (),                                                    # ⛔ ko 바이트 불변
}
# ⭐ 15차(2026-09-19, 사장님 지시 — 실통화 1657 ko t13 «…: "처음 뵙겠습니다." Can you try that?» 로 묻지 않고 정답부터 줬다):
#   DRILL_ASK_FIRST_LINE 은 이제 **전 언어 공통**이다(procedure 가 extra 뒤에 직접 붙인다). 여기서는 뺀다 — 두면 ja 가 같은 줄을 두 번 받는다.
#   ⚠ 문장은 한 글자도 안 바뀌었다(위치만 이동). ja 조립 바이트도 그대로다 — 공통 줄을 **extra 뒤**에 두어 종전 순서를 지켰다.
DRILL_EXTRA_LINES_DEFAULT: tuple[str, ...] = (DRILL_TARGET_SCRIPT_LINE,)   # ja 등 비ko
DRILL_SILENCE_LINE = '- 무음은 정답이 아니다. {locale_label}로 현재 원문 또는 현지인 문장을 다시 들려주고 답을 기다린다. 미완료 현지인 복창을 새 원문으로 바꾸지 않는다.'
# [퀴즈] — T16 큐 계약: «{CONTROL_TAG} 이 «지금 퀴즈를 내라» 고 알릴 때만». CONTROL_TAG 는 조립 때 끼운다.
QUIZ_HEADER = "[퀴즈]"
QUIZ_LINE_1 = '- 퀴즈는 {control_tag} 이 «지금 퀴즈를 내라» 고 알릴 때만 낸다 — 스스로 퀴즈·복습·테스트를 시작하지 마라. 알림이 오면 퀴즈를 시작한다는 말을 {locale_label}로 먼저 하고, 거기 적힌 표현들을 한 문제씩, {locale_label}로 상황을 주고 {target}로 말하게 하라. **한 번에 한 문제씩** 묻고 답을 기다려라. 새 표현을 만들지 마라 — 정답은 그 묶음에서 배운 표현이다.'
QUIZ_LINE_2 = "- 틀리면 힌트(첫 음절·상황·뜻)만 — **표현 전체나 그 어절을 말하지 마라.** 그래도 못 하면 정답을 들려주고 따라 말하게 해라 — **그리고 거기서 멈추고 기다려라.** 다음 문제는 학습자가 따라 말한 다음 턴에 낸다. 정답을 들려준 뒤에는 '어떻게 말해요?' 로 되묻지 마라."

# [오늘의 표현] 목록 머리·언어 안내·재료 소진(«끝» 이 아니라 «다음» — call 870).
ITEMS_HEADER = '[오늘의 표현 — 이 목록을 번호 순서대로 다뤄라]'
ITEMS_LANGUAGE_NOTE = '⚠ 적힌 언어는 네가 말할 언어와 무관하다 — 여기 적힌 표현·예문만 {target}로 또박또박 들려주고, 그것을 꺼내는 말·지시·반응·뜻 설명은 전부 {locale_label}로 해라. 목록의 존재·남은 개수·진행률은 학습자에게 발설하지 마라.'
ITEMS_EXHAUSTION_LINE = '- 재료를 다 쓴 뒤에도 대화는 그대로 이어진다 — 오늘 다룬 표현들이 서로 어떻게 다르고 언제 쓰는지 {locale_label}로 한두 문장만 짚어 주고, 학습자가 그중 하나를 넣은 문장을 직접 만들어 말하게 해라 — 표현을 바꿔 가며 계속 이어가라.'

# [반응] — «맞았다고 하지는 마라» 를 판정이 기대한다.
CHARACTER_FRAME = '[반응 — 네 캐릭터로 한다]\n- 맞혔을 때: 캐릭터대로 짧게 반응하고 원문 정답은 현지인 복창으로, 현지인 정답은 다음 항목으로 이어간다.\n- 틀렸을 때: 현재 문장을 다시 들려주고 각각 두 시도까지 기다린다. 소진 뒤 다음 항목으로 넘어가되 맞았다고 하지 않는다.\n- 말투는 캐릭터대로 유지하며 학습 순서와 언어는 코스 규칙이 우선한다.'

# [3.1 말투] — T21-B. 2.5 는 빈 문자열(바이트 동일). 줄 5·6 에 {target}·{locale_label} 슬롯.
MODEL_BLOCK_31_LINES: tuple[str, ...] = (
    '[3.1 말투]',
    '- 턴은 짧게 한다. 첫 인사 뒤 첫 항목을 알려주고 복창 요청 뒤 기다린다.',
    '- 학습자 이름은 위 [페르소나]에 적힌 대화상대 이름만 부른다 — 없으면 부르지 마라. 다른 이름을 지어내지 마라.',
    '- 같은 문장·같은 농담을 두 번 쓰지 마라 — 반응은 매번 다르게.',
    '- {target}로 말하는 모든 것은 정중형이다 — 작별 인사도.',
    '- 뜻·상황은 {locale_label}로 짧게 설명하고, 현지인 복창 문장 하나는 전사에서 구별한다.',
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


def procedure(*, drill_intro: str, target: str, locale_label: str, has_grammar: bool = False, language: str = "ko") -> str:
    """[진행 절차] + [퀴즈]. drill_intro(편집 문구, 슬롯 치환 전)가 첫 불릿이고 나머지는 잠금. language 는 격식 줄·비ko 전용 줄(C1·C2)을 가른다(ko 는 종전 바이트)."""
    fmt = dict(target=target, locale_label=locale_label)
    formality = DRILL_FORMALITY_LINE_BY_LANGUAGE.get(language, DRILL_FORMALITY_LINE)
    extra = DRILL_EXTRA_LINES_BY_LANGUAGE.get(language, DRILL_EXTRA_LINES_DEFAULT)
    return "\n".join([
        PROCEDURE_HEADER,
        drill_intro.format(**fmt),
        *[line.format(**fmt) for line in extra],
        DRILL_ASK_FIRST_LINE.format(**fmt),          # 15차 — 전 언어 공통(ko 추가·ja 중복 제거). 자리는 종전 ja 와 같은 맥락(drill_intro 뒤)
        *([DRILL_GRAMMAR_LINE, DRILL_GRAMMAR_ALT_LINE] if has_grammar else []),
        DRILL_REVEAL_LINE,
        formality.format(**fmt),
        DRILL_ALT_CORRECT_LINE,
        DRILL_ONE_ASK_LINE,
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
