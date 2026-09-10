"""프리토킹(freetalk) 통화 대본 조립 — 순수 문자열, LLM 생성 0.

**100% 학습 언어**로 자유대화만 한다(D8). 학습 항목·체크판·힌트·진도 기여가 **하나도 없다** —
페르소나와 캐릭터로만 대화를 끌고 간다.

## ⛔ 이 코스에 없는 것 (일부러 없다)
- **학습 항목 블록**: 주제 선정·배운 표현 활용은 **미기획**이다(기획서 §1 비범위).
  ⛔ 여기에 임의로 항목을 실지 마라 — 그건 다음 기획의 자리다.
- **밴드 정책**: 일반 통화의 밴드 정책은 «초보에게 모국어 발판을 얼마나 댈지»의 규칙인데,
  이 코스엔 발판이 없다. 대신 막히면 **같은 언어 안에서 더 쉽게** 바꿔 말한다.
- **진도·증거**: 이 통화는 체크판에 아무것도 쓰지 않는다.

## ⛔ 규칙 번호를 5·6·7 로 고정한다
`RULE_NONVERBAL_SOUND`(6)와 `RULE_OFF_TOPIC`(7)이 본문에서 "규칙 5"를 인용한다 —
번호를 다시 매기면 그 인용이 끊긴다(core/prompts/common.py 주석).

설계: docs/plans/2026-09-10-표현학습-프리토킹-통화-분리.md §1·D8
"""

from __future__ import annotations

from core.prompts.common import (
    CLOSE_TAG_DEFAULT,
    CONTROL_TAG,
    DEFAULT_MAX_SENTENCES,
    PERSONA_TAIL,
    RULE_CLOSE_PROTOCOL,
    RULE_NONVERBAL_SOUND,
    RULE_OFF_TOPIC,
    RULE_RESPONSE_LENGTH,
    locale_label as _locale_label,
)


# --------------------------------------------------------------------------- #
# 선톡 시드
# --------------------------------------------------------------------------- #
# ⭐ 일반 통화의 «공부할래 수다 떨래?» 모드 질문이 없다 — 통화 종류가 이미 그 답이다.
# ⚠ 첫 인사만 예외적으로 모국어를 허용할지 고민했지만 **하지 않았다**: 이 코스의 약속이
#   «전부 학습 언어» 이고, 첫 턴에 모국어가 나가면 그 뒤로 계속 끌려간다(실측 계열: 규칙 3
#   ★[학습자 언어에 끌려가지 마라]가 막으려는 바로 그 현상).
def seed_freetalk_opening(target_language: str = "한국어") -> str:
    return (
        f"[통화 시작] 네가 학습자에게 먼저 전화를 건 상황이다. **{target_language}로** "
        "짧게 인사하고, 곧바로 가벼운 화제 하나를 꺼내 질문 하나로 끝내라. "
        "질문만 하고 학습자의 음성 대답을 기다려라. "
        "이 [통화 시작] 안내문 자체는 소리 내어 읽지 말고 내용만 반영해라."
    )


# --------------------------------------------------------------------------- #
# 무음 넛지 1단 (기획서 §2-9)
# --------------------------------------------------------------------------- #
# ⚠ 일반 통화와 **성격은 같고 언어만 다르다** — 새 화제로 이어가되 학습 언어로 한다.
#   (표현학습과 달리 여기서는 «화제를 바꾸지 마라»가 필요 없다. 다룰 항목이 없다.)
# ⛔ 접두어는 CONTROL_TAG(종료 아님) — 종료 태그와 절대 공유하지 마라(call_id=683).
NUDGE_SEED_1_FREETALK = (
    f"{CONTROL_TAG} 학습자가 잠깐 조용하다. 이 메시지는 소리내 읽지 말고, 작별하지 말고 "
    "학습 언어로 가볍게 새 화제 한 문장만 이어가라."
)


# ⛔ 리터럴 학습자 대사를 넣지 마라(원칙 4). 예전 초안은 «학습자가 "이거 어떻게 말해요?"라고
#   물으면» 이라고 **한국어 대사를 박아** 뒀는데, 프랑스어 타깃 통화에도 그 한국어가 그대로
#   실린다(call 1097 이 정확히 그 사고였다 — 박힌 한국어 예문 하나가 설명 언어를 뒤집었다).
#   ⇒ 대사가 아니라 **질문의 성질**로 쓴다.
_RULE1_COURSE = """1. 이 통화는 자유대화다. 학습자의 관심사로 화제를 골라 대화를 이어가라 — 가르치는 시간이 아니다. 학습자가 어떤 말을 {target}로 어떻게 하는지 물으면 알려 주고, 곧바로 대화로 돌아와라."""

# ⭐⭐ 이 코스의 전부다: **전부 학습 언어**.
# ⛔ 모국어 발판(밴드 정책)을 넣지 마라 — 그러면 일반 통화가 된다. 막힘 처방은 «모국어로
#   풀어주기»가 아니라 «같은 언어 안에서 더 쉽게»다.
# ⚠ 단 하나의 예외를 남긴다(이해가 비율보다 우선): 학습자가 정말 못 알아들어 대화가
#   멈추면 그때만 한 마디 모국어를 쓰고 즉시 돌아온다. 이 예외가 없으면 초보와의 통화가
#   통째로 막힌다(R5 와 같은 논리 — 기능이 죽는 것보다 낫다).
# ⛔⛔ **사장님 승인(2026-09-10 결정 A) — 그리고 여기까지다.** 모국어가 열리는 자리는
#   딱 둘뿐이다: ① 학습자가 «이거 어떻게 말해요?» 를 물을 때(_RULE1_COURSE) ② 대화가
#   정말 멈췄을 때 한 마디(이 줄). 그 밖에 발판을 늘리지 마라 — 늘리는 순간 이 코스는
#   일반 통화가 되고, 이 코스의 존재 이유(D8 «전부 학습 언어»)가 사라진다.
#   ⚠ 예외를 조건절에서 떼지 마라("막히면 모국어로 도와라" 식의 무조건 허용). 조건이
#     사라지면 모델은 그걸 상시 허가로 읽는다 — 회귀
#     `test_freetalk_native_language_stays_a_stuck_only_exception` 이 이 자리를 잠근다.
_RULE3_LANGUAGE = """3. 언어 사용 — 매우 중요:
   - 이 통화는 처음부터 끝까지 {target}로 한다. 인사·질문·리액션·설명 전부 {target}다.
   - 학습자가 막히면 {locale_label}로 풀어 주지 말고, **더 쉬운 {target}**로 바꿔 말해라 — 짧은 문장, 쉬운 낱말, 또는 {target}로 된 선택지 두 개. 그래도 대화가 멈추면 그때만 {locale_label}로 한 마디 거들고 곧바로 {target}로 돌아와라.
   - 네 턴은 맨 끝을 {target} 질문·요청으로 착지시켜라(물음표로 끝내고 멈춰라). 학습자는 네 마지막 말의 언어로 답한다.
   - 네가 던진 질문의 답을 같은 턴에 스스로 말하지 마라(자문자답 금지). 질문 뒤엔 조용히 기다려라.
   - 학습자 차례를 {target} 산출 0으로 끝내지 마라."""

_RULE4_CORRECTION = """4. 교정 스타일:
   - 대화가 우선이다 — 교정은 한 번에 1개까지만, 뜻이 안 통할 때만. 사소한 것까지 잡는 과교정은 금지.
   - 고칠 때는 올바른 {target} 표현을 단독으로 또박또박 한 번 들려주고 곧바로 대화를 이어가라 — 감싸는 말투는 네 캐릭터대로(공손한 "이렇게 말해요"를 강요하지 마라)."""

_FREETALK_TEMPLATE = (
    """너는 '비버' — 아래 [페르소나]의 인물이다. 그 인물로서 외국인 학습자에게 전화를 걸어 {target}로만 대화한다.

[모국어] 학습자의 모국어는 {locale_label}다.

[페르소나] 네 역할은 "{role}"다. 말투·성격: {personality}
{target}로 대화하고 이따금 고쳐 주는 건 네가 하는 '일'일 뿐, 네 말투·성격은 오직 그 캐릭터다 — 통화가 길어져도 처음의 강도를 끝까지 유지하라. """
    + PERSONA_TAIL + """

[불변 규칙 — 캐릭터와 무관하게 항상 지켜라]
"""
    + _RULE1_COURSE + "\n2. " + RULE_CLOSE_PROTOCOL + "\n"
    + _RULE3_LANGUAGE + "\n"
    + _RULE4_CORRECTION + "\n"
    + RULE_RESPONSE_LENGTH + "\n"
    + RULE_NONVERBAL_SOUND + "\n"
    + RULE_OFF_TOPIC
)


def build_freetalk_instruction(
    *,
    role: str,
    personality: str,
    level_profile: str,
    locale: str,
    interests: list[str],
    name: str | None = None,
    target_language: str = "한국어",
    locale_label: str | None = None,
    close_tag: str = CLOSE_TAG_DEFAULT,
    max_sentences: int | None = None,
) -> str:
    """프리토킹 통화의 system_instruction 을 조립한다(LLM 생성 0).

    ⚠ `level_profile` 은 **싣는다** — 항목은 안 주지만 «이 학습자에게 어느 정도 난이도로
      말할지»는 알아야 한다. 그게 없으면 초보에게 고급 문장이 나간다.
    ⚠ `close_tag` 는 출력에 실리지 않는다(서버 전용 — 지시문이 태그를 보여주면 비버가
      복사해 스스로 종료한다, call 852). 시그니처 대칭을 위해 받는다.
    """
    max_sentences = DEFAULT_MAX_SENTENCES if not max_sentences else max(1, int(max_sentences))
    label = _locale_label(locale, locale_label)
    interests_text = ", ".join(i for i in interests if i) or "일상"
    username = (name or "").strip() or "학습자"

    parts = [
        _FREETALK_TEMPLATE.format(
            target=target_language,
            locale_label=label,
            role=role or "친근한 한국어 대화 파트너",
            personality=personality or "다정하고 편안한 말투",
            username=username,
            max_sentences=max_sentences,
        ),
        f"\n[학습자 수준]\n{level_profile}",
        f"\n[학습자 흥미·소재] {interests_text}",
    ]
    return "\n".join(parts)
