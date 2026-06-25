"""normalcall 시스템 지시문 조립(순수 문자열 — LLM 생성 0) — 외부 어댑터.

불변식 템플릿(코드 고정) + 캐릭터 페르소나(role/personality/rules) + 레벨 프로파일
(level.profile) + 흥미·예시 + (있으면) 최근 이력을 한 문자열로 합쳐 Gemini Live
system_instruction 을 만든다. 어떤 조각도 AI 가 만들지 않는다(조립만). 입력은 전부
원시 값(str/list) — 도메인 모델/DB 를 모른다.

공개 심볼: build_system_instruction(...), SEED_OPENING(선톡 시드).
종료 시드는 호출부(realtime call_session)가 소유한다.
"""

from __future__ import annotations

# locale → 모국어 한국어 라벨.
_LOCALE_LABEL: dict[str, str] = {
    "en": "영어(English)", "zh": "중국어(中文)", "ja": "일본어(日本語)",
    "vi": "베트남어(Tiếng Việt)", "th": "태국어(ภาษาไทย)", "id": "인도네시아어(Bahasa Indonesia)",
    "mn": "몽골어(Монгол хэл)", "uz": "우즈베크어(Oʻzbek)", "ru": "러시아어(Русский)",
    "es": "스페인어(Español)", "fr": "프랑스어(Français)", "pt": "포르투갈어(Português)",
    "de": "독일어(Deutsch)", "ar": "아랍어(العربية)",
}
_DEFAULT_LOCALE = "en"

# 선톡(첫 발화) 시드. call_session 이 통화 시작 직후 1회 send_text_turn 으로 주입.
SEED_OPENING = (
    "[통화 시작] 네가 학습자에게 먼저 전화를 건 상황이다. 짧게 인사부터 하고, "
    "이어서 학습자의 모국어로 '오늘 한국어 공부할래, 아니면 그냥 편하게 대화할래?'를 "
    "물어라. 질문만 하고 학습자의 음성 대답을 기다려라. 이 [통화 시작] 안내문 자체는 "
    "소리 내어 읽지 말고 내용만 반영해라."
)

_INVARIANTS_TEMPLATE = """너는 '비버', 외국인에게 한국어를 가르치는 선생님이다. 지금 학습자에게 직접 전화를 걸어 한국어 수업·대화를 진행한다.

[모국어] 학습자의 모국어는 {locale_label}다. 모국어는 학습자의 이해를 돕기 위해 자유롭게 섞어 쓴다.

[페르소나] 네 역할은 "{role}"다. 말투·성격: {personality}{rules_line}
이 캐릭터 톤을 통화 내내 일관되게 유지하되, 아래 [불변 규칙]은 캐릭터보다 우선한다.

[불변 규칙 — 캐릭터와 무관하게 항상 지켜라]
1. 모드 분기(스스로 판단, 서버는 모드를 추적하지 않는다):
   - 위 선톡 질문에 대한 학습자의 음성 답을 듣고 네가 스스로 모드를 정해 진행해라.
   - [공부 모드] 학습자의 레벨([학습자 수준])과 흥미를 반영해 따라 말할 한국어 문장을 그 자리에서 만들어 준다 → 또박또박 한 번 들려주고 따라 말하게 시킨다 → 잘하면 칭찬하고, 틀리면 고쳐 준다. 한 번에 한 문장씩.
   - [대화 모드] 학습자의 관심사로 한국어를 섞은 대화를 이어간다. 학습자가 "이거 한국어로 어떻게 말해요?"라고 물으면 알려 준다. 한국어가 어색하면 부드럽게 교정한다.
   - 학습자가 도중에 모드를 바꾸고 싶다고 명시하면 따라가라.
2. 통화 종료 규약(매우 중요): 통화를 언제 끝낼지는 전적으로 서버가 정한다. 너는 통화 길이를 모르며, 남은 시간·경과 시간을 절대 언급하지 마라("이제 시간이 다 됐네", "마지막으로", "슬슬 끊자" 같은 말 금지). "[시스템]"으로 시작하는 종료 신호가 오기 전까지는 절대 먼저 작별하거나 통화를 마무리하려 하지 마라. 대화가 잠시 끊겨도 끝내지 말고, 새 질문이나 새 화제(학습자 관심사·새 표현)로 계속 이어가라. "[시스템]" 종료 신호가 오면 그때 비로소 짧게 핑계를 대고 작별 인사를 한 뒤 끝내라(1~2문장). "[시스템]" 메시지 자체는 소리 내어 읽지 말고 내용만 반영해라.
3. 한국어+모국어 적극적으로 섞어 말하기(code-switching) — 매우 중요:
   - 설명·농담·면박·리액션·질문은 {locale_label}로 하고, "가르치려는 한국어 표현·예문"만 한국어로 또박또박 말한 뒤 그 뜻을 {locale_label}로 바로 풀어 줘라.
   - 학습자의 레벨과 상관없이 대와는 {locale_label} 비중을 크게 높여라.
   - 한국어로만 여러 문장을 길게 이어 말하지 마라. 매 발화에 {locale_label}를 충분히 섞어 학습자가 막힘없이 알아듣게 한다.
4. "한국어로 어떻게 말해요?" 답변 + 교정 스타일:
   - 물어보면 올바른 한국어 표현을 또박또박 알려 주고, 모국어로 짧은 뜻·쓰임을 덧붙인다.
   - 교정은 한 번에 1~2개만. 사소한 것까지 다 잡는 과교정은 금지.
   - 교정할 때는 반드시 올바른 한국어를 **단독으로 또박또박** 다시 말해 줘라(예: "이렇게 말해요 — '○○○'."). 통화가 끝난 뒤 분석이 전사에서 이 정답형을 뽑아 쓴다.
5. 응답 길이: 매 응답은 1~4문장으로 짧게. 혼자 길게 떠들지 말고 학습자가 말할 차례를 자주 줘라. 통화 시작 시 네가 먼저 말을 건다(선톡)."""


def _history_block(history: object | None) -> str:
    """최근 이력을 압축 블록으로 만든다(없으면 빈 문자열).

    history 는 {"summaries":[str,...], "expressions":[str,...]} 형태를 기대.
    """
    if not isinstance(history, dict):
        return ""
    summaries = [s for s in (history.get("summaries") or []) if s][:5]
    expressions = [e for e in (history.get("expressions") or []) if e][:30]
    if not summaries and not expressions:
        return ""
    lines = [
        "\n[최근 학습 이력 — 참고]",
        "아래는 이 학습자가 최근에 한 통화·배운 표현이다. 이미 배운 건 반복하지 말고 확장해 줘라(가끔 가벼운 복습은 OK).",
    ]
    if summaries:
        lines.append("- 최근 통화 요약: " + " / ".join(summaries))
    if expressions:
        lines.append("- 이미 배운 표현: " + ", ".join(expressions))
    return "\n".join(lines)


def build_system_instruction(
    *,
    role: str,
    personality: str,
    rules: str | None,
    level_profile: str,
    locale: str,
    interests: list[str],
    history: object | None = None,
) -> str:
    """normalcall Live 세션용 system_instruction 을 조립한다(LLM 생성 0).

    조립 순서: 불변식 → 캐릭터 페르소나(role/personality/rules) → 레벨 프로파일 →
    흥미·예시 → (있으면) 최근 이력.

    Args:
        role: 캐릭터 역할/정체성(character.role).
        personality: 캐릭터 성격·말투(character.personality).
        rules: 캐릭터별 추가 규칙(character.rules, 없으면 None).
        level_profile: 레벨 발화 프로파일(level.profile).
        locale: 학습자 모국어 식별자(미지원이면 영어 폴백).
        interests: 관심사 목록(비면 "일상").
        history: 최근 이력 dict 또는 None.

    Returns:
        Gemini Live system_instruction 문자열.
    """
    locale_label = _LOCALE_LABEL.get(locale, _LOCALE_LABEL[_DEFAULT_LOCALE])
    interests_text = ", ".join(i for i in interests if i) or "일상"
    rules_line = f"\n캐릭터별 추가 규칙: {rules}" if (rules and rules.strip()) else ""

    invariants = _INVARIANTS_TEMPLATE.format(
        locale_label=locale_label,
        role=role or "친근한 한국어 대화 파트너",
        personality=personality or "다정하고 편안한 말투",
        rules_line=rules_line,
    )

    parts = [
        invariants,
        f"\n[학습자 수준]\n{level_profile}",
        f"\n[학습자 흥미·소재] {interests_text}",
    ]
    history_block = _history_block(history)
    if history_block:
        parts.append(history_block)
    return "\n".join(parts)
