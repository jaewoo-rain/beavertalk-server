"""프리토킹(freetalk) 통화 대본 조립 — 순수 문자열, LLM 생성 0.

**100% 학습 언어**로 자유대화만 한다(D8). 체크판·힌트·진도 기여가 **하나도 없다** —
페르소나와 캐릭터로만 대화를 끌고 간다.

## 두 벌의 대본 (2026-09-12 프리토킹 코스 v1 — docs/plans/2026-09-12-프리토킹-코스-대본.md §3·§9)
- `lesson=None`(옛 경로·CUR_ENABLED=false) — 옛 대본 **바이트 그대로**(아래 `_FREETALK_TEMPLATE`). 폐기 예정이지만 시험이 지킨다.
- `lesson=CurFreetalkBrief` — 커리큘럼 2단계 프리토킹: **역할극**(사장님 2026-09-12 실통화 뒤 결정 — 처음 «과제 시키기» 판을 뒤집었다:
  «프리토킹인데 저렇게 하는 게 아니라 역할극 한다고 생각하고 하자. 물어볼 때는 선생님으로서 알려주고. 그냥 대화한다고 생각해야 해»).
  비버는 [이번 차시] «상대» 인물이 되어 그 상황 속에서 그냥 대화한다 — 말투·성격은 캐릭터 그대로. 학습자가 뜻을 묻거나 막히면 **그 턴만
  선생님으로 돌아와**(규칙 3 예외) 알려주고 다시 역할로. 차시 항목 전부(문형은 예문과 함께)가 소재다 — 가르치지 않고 판정·퀴즈도 없다. 규칙 1·3·4 만
  코스판(짧은 v1 — «예/아니요 뒤 열린 질문»·«같은 과제 예외 1회»·«목록 안 낱말» 같은 세부 절은 실측에서 실패가 보이면 그때 한 절씩)이고
  규칙 2·5·6·7·PERSONA_TAIL 은 common 바이트 그대로. [학습자 흥미] 블록은 없다(소재는 차시 — 표현학습 T21-A 선례).

## ⛔ 이 코스에 없는 것 (일부러 없다)
- **학습 항목 «가르치기»**: 옛 대본엔 항목 블록 자체가 없고, 차시판의 [이번 차시] 는 **소재**다(«문형은 이름을 말하지 말고 문장으로»).
  ⛔ 여기에 드릴·따라 말하기·판정을 실지 마라 — 그건 표현학습(core/prompts/expression.py)이다.
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
from core.prompts.locked.freetalk import PROBE_NAME_RE, lesson_block


def _ed(key: str) -> str:
    """editable/freetalk.md 의 섹션(사람이 고치는 문구). 검사 실패 시 로더가 기본판으로 폴백한다."""
    return _section("freetalk", key)



# --------------------------------------------------------------------------- #
# 선톡 시드
# --------------------------------------------------------------------------- #
# ⭐ 일반 통화의 «공부할래 수다 떨래?» 모드 질문이 없다 — 통화 종류가 이미 그 답이다.
# ⚠ 첫 인사만 예외적으로 모국어를 허용할지 고민했지만 **하지 않았다**: 이 코스의 약속이
#   «전부 학습 언어» 이고, 첫 턴에 모국어가 나가면 그 뒤로 계속 끌려간다(실측 계열: 규칙 3
#   ★[학습자 언어에 끌려가지 마라]가 막으려는 바로 그 현상).
# ⭐ 잠금/편집 분리(2026-09-12): 선톡 시드 말투는 editable/freetalk.md `seed_opening_old` / `seed_opening_lesson`(슬롯 {target}).
def seed_freetalk_opening(target_language: str = "한국어") -> str:
    return _ed("seed_opening_old").format(target=target_language)


def seed_freetalk_lesson_opening(target_language: str = "한국어") -> str:
    return _ed("seed_opening_lesson").format(target=target_language)


# ⭐ 차시판(커리큘럼 2단계 프리토킹) 선톡 — 역할극(계획 §10): «상대» 인물로서 첫 말을 건다(인사 + 상황 속 첫 질문 하나).
# ⛔ 옛 `seed_freetalk_opening` 은 한 글자도 안 바뀐다(lesson=None 경로).


# ⭐ 잠금 분리(2026-09-12): NUDGE_SEED_1_FREETALK, NUDGE_SEED_1_FREETALK_LESSON, NUDGE_SEED_2_FREETALK → core/prompts/locked/seeds.py 로 **이동**(복사 아님 — 바이트 그대로).
from core.prompts.locked.seeds import (
    NUDGE_SEED_1_FREETALK,
    NUDGE_SEED_1_FREETALK_LESSON,
    NUDGE_SEED_2_FREETALK,
)


# ⭐ 잠금 분리(2026-09-12): build_freetalk_reground_brief → core/prompts/locked/reground.py 로 **이동**(복사 아님 — 바이트 그대로).
from core.prompts.locked.reground import (
    build_freetalk_reground_brief,
)


# ⛔ 리터럴 학습자 대사를 넣지 마라(원칙 4). 예전 초안은 «학습자가 "이거 어떻게 말해요?"라고
#   물으면» 이라고 **한국어 대사를 박아** 뒀는데, 프랑스어 타깃 통화에도 그 한국어가 그대로
#   실린다(call 1097 이 정확히 그 사고였다 — 박힌 한국어 예문 하나가 설명 언어를 뒤집었다).
#   ⇒ 대사가 아니라 **질문의 성질**로 쓴다.
# ⭐ 잠금/편집 분리(2026-09-12) — 옛 경로 규칙 1·3·4·페르소나 문단은 editable/freetalk.md(`*_old`). 조립 결과 바이트 동일(기준 해시 시험).
_RULE1_COURSE = _ed("rule1_old")
_RULE3_LANGUAGE = _ed("rule3_old")
_RULE4_CORRECTION = _ed("rule4_old")
_FREETALK_TEMPLATE = (
    _ed("persona_intro_old") + " " + PERSONA_TAIL + """

[불변 규칙 — 캐릭터와 무관하게 항상 지켜라]
"""
    + _RULE1_COURSE + "\n2. " + RULE_CLOSE_PROTOCOL + "\n"
    + _RULE3_LANGUAGE + "\n"
    + _RULE4_CORRECTION + "\n"
    + RULE_RESPONSE_LENGTH + "\n"
    + RULE_NONVERBAL_SOUND + "\n"
    + RULE_OFF_TOPIC
)


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


# =========================================================================== #
# 차시판(커리큘럼 2단계 프리토킹, 2026-09-12) — lesson 이 있을 때만. 계획 §3 전문 중 규칙 1·3·4 는 v1 짧은 판(§9 정정).
# =========================================================================== #
#: 차시 프리토킹의 응답 길이(규칙 5 슬롯). 사장님 ⑥ «한 턴 길이 통일» + 1325 실측(요청 2개면 하나를 버린다) + A1 음성 처리 5~10음절.
#   호출부(call_session cur 프리토킹 분기)가 이 값을 넘긴다 — 옛 경로 기본값(DEFAULT_MAX_SENTENCES)은 그대로.
FREETALK_MAX_SENTENCES = 2

# ⛔ 리터럴 학습자 대사 0(원칙 4 — call 1097) · 톤 부사 0(원칙 1) · 종료 어휘 0(원칙 5). 부탁은 «성질» 로만 쓴다.
# ⭐ 역할극(§10, 2026-09-12 사장님 실통화 뒤): 옛 v1 «과제를 직접 던진다·너 자신으로 받는다·연기하지 마라» 를 뒤집었다 — 비버가 «상대» 인물이
#   되어 그냥 대화한다. 과제·연습 어휘는 이 대본에서 0(시험이 잠근다).
# ⭐ 잠금/편집 분리(2026-09-12) — 차시판 규칙 1·3·4·페르소나 문단·[이번 차시] 문장은 editable/freetalk.md(`*_lesson`, `lesson_*`),
#   블록 구조(항목 렌더·probes 이름 치환·상대 폴백)는 잠금 locked/freetalk.py. 조립 결과 바이트 동일.
_RULE1_LESSON = _ed("rule1_lesson")
_RULE3_LESSON = _ed("rule3_lesson")
_RULE4_LESSON = _ed("rule4_lesson")
_FREETALK_LESSON_TEMPLATE = (
    _ed("persona_intro_lesson") + " " + PERSONA_TAIL + """

[불변 규칙 — 캐릭터와 무관하게 항상 지켜라]
"""
    + _RULE1_LESSON + "\n2. " + RULE_CLOSE_PROTOCOL + "\n"
    + _RULE3_LESSON + "\n"
    + _RULE4_LESSON + "\n"
    + RULE_RESPONSE_LENGTH + "\n"
    + RULE_NONVERBAL_SOUND + "\n"
    + RULE_OFF_TOPIC
)
_PROBE_NAME_RE = PROBE_NAME_RE


def _lesson_block_v1(lesson: object, *, username: str) -> str:
    """«[이번 차시 — 이 상황을 역할극으로 대화한다]» 블록 — 구조는 잠금(lesson_block), 문장은 편집 파일."""
    return lesson_block(
        lesson, username=username,
        header=_ed("lesson_header"), partner_line=_ed("lesson_partner_line"), partner_fallback=_ed("lesson_partner_fallback"),
        material_line=_ed("lesson_material_line"), probes_prefix=_ed("lesson_probes_prefix"),
    )


# ⭐ 예외는 «그 턴만 선생님으로» 이고 착지는 학습 언어 요청이다. 다음 턴부터 다시 역할·전부 학습 언어 — 학습자 언어에 끌려가지 마라(규칙 3 ★ 계열).

# recast 만 — 명시 교정 0(linguist §5 · conv §5). 흐름 > 정확성.


# probes 의 시드 고유명(«마이클 씨») → 학습자 이름. 이름 환각의 반대 방향 위험(T21-B)을 막는다. 한글·라틴 1~10자 + « 씨».


def _build_freetalk_lesson_instruction(
    *, role: str, personality: str, level_profile: str, locale: str, name: str | None,
    target_language: str, locale_label: str | None, max_sentences: int | None, lesson: object,
) -> str:
    """차시판 조립 — 흥미 블록 없음(interests 미주입). 규칙 5 의 문장 수는 호출부 값(기본 DEFAULT_MAX_SENTENCES; cur 분기는 FREETALK_MAX_SENTENCES)."""
    max_sentences = DEFAULT_MAX_SENTENCES if not max_sentences else max(1, int(max_sentences))
    label = _locale_label(locale, locale_label)
    username = (name or "").strip() or "학습자"
    parts = [
        _FREETALK_LESSON_TEMPLATE.format(
            target=target_language,
            locale_label=label,
            role=role or "친근한 한국어 대화 파트너",
            personality=personality or "다정하고 편안한 말투",
            username=username,
            max_sentences=max_sentences,
        ),
        f"\n[학습자 수준]\n{level_profile}",
        "\n" + _lesson_block_v1(lesson, username=username),
    ]
    return "\n".join(parts)


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
    lesson: object | None = None,
    face_rule: str = "",
) -> str:
    """프리토킹 통화의 system_instruction 을 조립한다(LLM 생성 0).

    ⚠ `level_profile` 은 **싣는다** — 항목은 안 주지만 «이 학습자에게 어느 정도 난이도로
      말할지»는 알아야 한다. 그게 없으면 초보에게 고급 문장이 나간다.
    ⚠ `close_tag` 는 출력에 실리지 않는다(서버 전용 — 지시문이 태그를 보여주면 비버가
      복사해 스스로 종료한다, call 852). 시그니처 대칭을 위해 받는다.
    ⭐ `lesson`(커리큘럼 2단계, 2026-09-12): `curriculum_service.CurFreetalkBrief`(situation · partner · items[{obj, ex, role}] · probes).
      None 이면 **옛 대본 바이트 동일**(옛 경로·스냅샷). 있으면 **차시판 대본**(`_FREETALK_LESSON_TEMPLATE` + [학습자 수준] + [이번 차시]) —
      규칙 1·3·4 가 코스판이고 흥미 블록이 없다(`interests` 는 시그니처 호환으로만 받는다). 판정·퀴즈는 없다.
      ⛔ 표현을 «가르치라» 고 쓰지 마라 — 그건 표현학습이다. 여기서 표현 목록은 대화 소재다.
    """
    if lesson is not None:
        return _build_freetalk_lesson_instruction(
            role=role, personality=personality, level_profile=level_profile, locale=locale, name=name,
            target_language=target_language, locale_label=locale_label, max_sentences=max_sentences, lesson=lesson,
        )
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
    if face_rule:
        # ⭐ 표정 규칙(2026-09-12 ctx-lab): 빈 문자열(기본)이면 안 붙어 종전과 바이트 동일.
        parts.append("\n" + face_rule)
    return "\n".join(parts)
