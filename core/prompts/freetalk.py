"""프리토킹(freetalk) 통화 대본 조립 — 순수 문자열, LLM 생성 0.

**100% 학습 언어**로 자유대화만 한다(D8). 체크판·힌트·진도 기여가 **하나도 없다** —
페르소나와 캐릭터로만 대화를 끌고 간다.

## 두 벌의 대본 (2026-09-12 프리토킹 코스 v1 — docs/plans/2026-09-12-프리토킹-코스-대본.md §3·§9)
- `lesson=None`(옛 경로·CUR_ENABLED=false) — 옛 대본 **바이트 그대로**(아래 `_FREETALK_TEMPLATE`). 폐기 예정이지만 시험이 지킨다.
- `lesson=CurFreetalkBrief` — 커리큘럼 2단계 프리토킹: 선생님(비버 캐릭터)이 차시 상황의 말을 **과제로 직접 던지고**, 학습자의 말을
  **자기 자신으로** 받는다(역할극 없음). 차시 항목 전부(문형은 예문과 함께)가 소재다 — 가르치지 않고 판정·퀴즈도 없다. 규칙 1·3·4 만
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

import re

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


# ⭐ 차시판(커리큘럼 2단계 프리토킹) 선톡 — 계획 §3 «시드 3종» 선톡 문구 그대로. 인사 + 상황 한 문장 + 첫 과제 하나.
# ⛔ 옛 `seed_freetalk_opening` 은 한 글자도 안 바뀐다(lesson=None 경로).
def seed_freetalk_lesson_opening(target_language: str = "한국어") -> str:
    return (
        f"[통화 시작] 네가 학습자에게 먼저 전화를 건 상황이다. **{target_language}로** "
        "인사와 함께 [이번 차시]의 상황이 무엇인지 한 문장으로 말하고, "
        f"곧바로 첫 과제 하나를 {target_language}로 던진 뒤 멈춰 학습자의 음성 대답을 기다려라. "
        "무엇을 할지 묻지 마라(이미 정해져 있다). "
        f"이 안내문이 적힌 언어와 무관하게 네 말은 전부 {target_language}다. "
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

# ⭐ 차시판 무음 시드(계획 §3 «시드 3종» 1단·2단 문구 그대로). 차시 프리토킹에서 무음은 «대화가 끊겼다» 가 아니라 «방금 과제를 못 하고
#   있다» 다 — 1단은 화제를 바꾸지 말고 **같은 과제를 더 쉽게**, 2단은 그 턴만 모국어 뜻 + 학습 언어 문장 하나 → 학습 언어로 청함.
#   (옛 경로·표현학습·일반의 1·2단 시드는 바이트 그대로 — 호출부가 state.nudge_seed_1/2 슬롯으로 코스별로 꽂는다.)
# ⛔ 접두어는 CONTROL_TAG — 종료 태그와 공유 금지(call 683).
NUDGE_SEED_1_FREETALK_LESSON = (
    f"{CONTROL_TAG} 학습자가 잠깐 조용하다. 이 메시지는 소리내 읽지 말고, 작별하지 말고, 화제를 바꾸지 말고, "
    "방금 던진 과제를 학습 언어로 더 쉽게 바꿔(짧은 말, 쉬운 낱말, 또는 둘 중 고르기) 한 번만 다시 던져라."
)
NUDGE_SEED_2_FREETALK = (
    f"{CONTROL_TAG} 학습자가 계속 조용하다. 이 메시지는 소리내 읽지 말고, 작별하지 말고, 화제를 바꾸지 마라. "
    "이번 한 턴만 학습자의 모국어로 방금 과제의 뜻과 학습자가 할 학습 언어 문장 하나를 통째로 들려준 뒤, "
    "학습 언어로 그 문장을 말해 보라고 청해라. 다음 턴부터는 다시 학습 언어다."
)


def build_freetalk_reground_brief(situation: str, unused: list[str], *, target: str = "한국어") -> str:
    """차시 프리토킹 전용 재접지 쪽지(계획 §3 «재접지» 문구). 120s 마다 호출부(call_session `_arm_reground`)가 얹는다.

    ⭐ 왜 따로 있나: 일반 브리프(`persona_prompt.build_reground_brief` chat 모드)는 «흥미를 느낄 새 질문을 하나 던져» 라 상황을 깬다 —
      프리토킹이 **과제 없이 잡담**으로 새는 1순위 원인이었다(계획 §2 재접지). 여기선 상황 재확인 + «전부 학습 언어» + 아직 안 쓴 소재만.
    ⚠ `unused` 는 판정이 아니다 — 이번 통화 비버 발화에 아직 안 나온 소재 몇 개(3~5). 비어 있으면 그 절을 뺀다. 카운트·정오 없음.
    ⛔ 접두어는 CONTROL_TAG(종료 아님).
    """
    parts = [f"{CONTROL_TAG} 지금은 «{situation}» 상황의 회화 연습이다. 전부 {target}로, 한 턴에 과제 하나."]
    picks = [u for u in unused if isinstance(u, str) and u.strip()][:5]
    if picks:
        parts.append("아직 안 쓴 소재: " + " · ".join(picks) + ".")
    parts.append("이 안내문은 읽지 말고 내용만 반영해라.")
    return " ".join(parts)


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


# =========================================================================== #
# 차시판(커리큘럼 2단계 프리토킹, 2026-09-12) — lesson 이 있을 때만. 계획 §3 전문 중 규칙 1·3·4 는 v1 짧은 판(§9 정정).
# =========================================================================== #
#: 차시 프리토킹의 응답 길이(규칙 5 슬롯). 사장님 ⑥ «한 턴 길이 통일» + 1325 실측(요청 2개면 하나를 버린다) + A1 음성 처리 5~10음절.
#   호출부(call_session cur 프리토킹 분기)가 이 값을 넘긴다 — 옛 경로 기본값(DEFAULT_MAX_SENTENCES)은 그대로.
FREETALK_MAX_SENTENCES = 2

# ⛔ 리터럴 학습자 대사 0(원칙 4 — call 1097) · 톤 부사 0(원칙 1) · 종료 어휘 0(원칙 5). 부탁은 «성질» 로만 쓴다.
_RULE1_LESSON = """1. 이 통화는 아래 [이번 차시]의 상황을 {target}로 해 보는 회화 연습이다. 과제는 네가 그 상황의 말을 **직접 던지는 것**이다 — 상황 속 질문을 하거나, 네 이야기를 한 문장 하고 학습자에게 되물어라. 학습자가 **질문을 만들어야** 하는 과제만, 학습자가 할 {target} 문장을 실어서 너에게 물어보라고 청해라. 학습자가 말하면 그 말을 **너 자신으로서** 받아 대답하고 되물어라 — 다른 인물이 되어 연기하지 마라. 설명·따라 말하기·정오 판정은 이 통화에 없다. 한 턴에 과제 하나."""

# ⭐ 예외는 «그 턴만» 이고 착지는 학습 언어 요청이다. 다음 턴부터 전부 학습 언어 — 학습자 언어에 끌려가지 마라(규칙 3 ★ 계열).
_RULE3_LESSON = """3. 언어 사용 — 매우 중요:
   - 이 통화는 처음부터 끝까지 {target}로 한다. 인사·과제·대답·되묻기·리액션 전부 {target}다.
   - 예외는 한 턴뿐이다. 학습자가 모르겠다고 하거나, 뜻을 묻거나, {locale_label}로 말하면 **그 턴만** {locale_label}로 뜻을 한 문장으로 풀어 주고 학습자가 할 {target} 문장 **하나**를 통째로 들려준 뒤, 그 턴 끝에서 {target}로 그 문장을 말해 보라고 청해라. 다음 턴부터는 다시 전부 {target}다 — 학습자 언어에 끌려가지 마라.
   - 네 턴은 맨 끝을 {target} 질문·요청으로 착지시켜라(물음표로 끝내고 멈춰라). 네가 던진 질문의 답을 같은 턴에 스스로 말하지 마라. 학습자 차례를 {target} 산출 0으로 끝내지 마라."""

# recast 만 — 명시 교정 0(linguist §5 · conv §5). 흐름 > 정확성.
_RULE4_LESSON = """4. 교정 스타일: 따로 고쳐 주지 마라. 학습자 말에 틀린 데가 있으면 네 대답 안에 올바른 {target} 형태를 넣어 되받고 대화를 이어가라."""

_FREETALK_LESSON_TEMPLATE = (
    """너는 '비버' — 아래 [페르소나]의 인물이다. 그 인물로서 외국인 학습자에게 전화를 걸어 {target}로만 대화한다.

[모국어] 학습자의 모국어는 {locale_label}다.

[페르소나] 네 역할은 "{role}"다. 말투·성격: {personality}
{target}로 대화하고 과제를 청하는 건 네가 하는 '일'일 뿐, 네 말투·성격은 오직 그 캐릭터다 — 통화가 길어져도 처음의 강도를 끝까지 유지하라. """
    + PERSONA_TAIL + """

[불변 규칙 — 캐릭터와 무관하게 항상 지켜라]
"""
    + _RULE1_LESSON + "\n2. " + RULE_CLOSE_PROTOCOL + "\n"
    + _RULE3_LESSON + "\n"
    + _RULE4_LESSON + "\n"
    + RULE_RESPONSE_LENGTH + "\n"
    + RULE_NONVERBAL_SOUND + "\n"
    + RULE_OFF_TOPIC
)

# probes 의 시드 고유명(«마이클 씨») → 학습자 이름. 이름 환각의 반대 방향 위험(T21-B)을 막는다. 한글·라틴 1~10자 + « 씨».
_PROBE_NAME_RE = re.compile(r"[가-힣A-Za-z]{1,10} 씨")


def _lesson_block_v1(lesson: object, *, username: str) -> str:
    """«[이번 차시 — 이 상황을 과제로 던진다]» 블록(계획 §3). items(obj·ex·role) 전부 — 문형은 «이름 — "예문"», 청크는 «표현», 어휘는 headword.

    ⚠ items 가 없는 옛 브리프(surfaces 만)는 그 표면형을 «표현» 줄로 싣는다(호환). 문법 0 인 차시(레벨1 청크)는 «문형:» 줄이 없다.
    ⛔ «나머지는 모국어로» 류 모순 문구를 되살리지 마라 — 이 코스는 처음부터 끝까지 학습 언어다(규칙 3).
    """
    situation = (getattr(lesson, "situation", None) or "").strip()
    partner = (getattr(lesson, "partner", None) or "").strip()
    items = [d for d in (getattr(lesson, "items", None) or []) if isinstance(d, dict) and (d.get("obj") or "").strip()]
    if not items:
        items = [{"obj": s, "ex": None, "role": "chunk"}
                 for s in (getattr(lesson, "surfaces", None) or []) if isinstance(s, str) and s.strip()]
    probes = [_PROBE_NAME_RE.sub(f"{username} 씨", p.strip())
              for p in (getattr(lesson, "probes", None) or []) if isinstance(p, str) and p.strip()]
    grammar = [d for d in items if d.get("role") == "grammar"]
    chunks = [d for d in items if d.get("role") == "chunk"]
    vocab = [d for d in items if d.get("role") not in ("grammar", "chunk")]

    lines = ["[이번 차시 — 이 상황을 과제로 던진다]"]
    if situation:
        lines.append(f"- 상황: {situation}")
    if partner:
        lines.append(f"- 상대: {partner} — 상황 묘사다. 너는 여전히 [페르소나]의 인물이고, 학습자의 말을 받는 것도 너 자신이다.")
    if items:
        lines.append("- 소재: 이 차시의 표현이다. 네 과제·대답·되묻기에 자연스럽게 섞어 써라. 문형은 이름을 말하지 말고 문장으로 써라.")
        if grammar:
            lines.append("  문형: " + " / ".join(
                f"{d['obj'].strip()} — \"{str(d['ex']).strip()}\"" if (d.get("ex") or "").strip() else d["obj"].strip()
                for d in grammar
            ))
        if chunks:
            lines.append("  표현: " + " · ".join(d["obj"].strip() for d in chunks))
        if vocab:
            lines.append("  어휘: " + " · ".join(d["obj"].strip() for d in vocab))
    if probes:
        lines.append("- 대화가 막히면 이런 질문으로 이끌어라(뜻만 참고해 네 말로): " + " / ".join(probes))
    return "\n".join(lines)


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
    return "\n".join(parts)
