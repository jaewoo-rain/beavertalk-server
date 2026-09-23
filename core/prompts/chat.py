"""자유대화(chat) 통화 대본 조립 — C7(2026-09-23, docs/plans/2026-09-22-프리미엄-자유대화
-15분-달력.md). 순수 문자열 조립, LLM 생성 0.

프리토킹(freetalk.py)의 D8 기본 대본(차시 블록 없음, `lesson=None`)을 그대로 재사용하고,
그 위에 새 블록 `[관심사]`(회원 실제 관심사, 없으면 생략) · `[기억]`(chat_memory 의
summary·facts·next_topics, 없으면 생략)을 얹는다. 새 문구가 필요해서 새 파일로 뺐다 —
freetalk.py(다른 코스가 같이 쓴다)나 잠금 파일은 안 건드린다.

⛔ `build_freetalk_instruction` 자체의 `interests` 인자는 **일부러 빈 리스트로 고정**
  한다 — 그 함수는 비어 있으면 "[학습자 흥미·소재] 일상"으로 떨어져(기존 동작, freetalk
  이 계속 그렇게 쓴다) **절대 생략되지 않는다.** 문서가 요구하는 "관심사 없으면 생략"은
  이 파일이 따로 만드는 `[관심사]` 블록이 담당한다(진짜 회원 관심사는 여기로만 흐른다).
"""

from __future__ import annotations

from core.prompts.freetalk import build_freetalk_instruction

# ⭐⭐ QA C7-③: "아는 척" 시드는 기억이 **있을 때만** 쓴다. 내용 없이 나가면 비버가
#   지어낸다 — memory_is_substantial 이 이 함수의 유일한 관문이다.
_MEMORY_OPENING_TEMPLATE = "오랜만이에요! 지난번에 {topic} 얘기했었잖아요 — 그거 어떻게 됐어요?"


def _pick_recall_topic(memory: dict) -> str:
    """기억에서 "아는 척" 시드에 쓸 화제 하나 — topics 우선(더 최근·더 화제스럽다), 없으면 facts."""
    for key in ("topics", "facts"):
        for item in memory.get(key) or []:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def memory_is_substantial(memory: dict | None) -> bool:
    """기억이 "아는 척" 할 만큼 있나 — 화제·사실이 하나도 없으면 빈약(D8 오프닝으로 폴백)."""
    if not memory:
        return False
    return bool(memory.get("topics") or memory.get("facts"))


def seed_chat_opening(target_language: str, memory: dict | None) -> str:
    """기억이 빈약하면 빈 문자열 — 호출부가 `seed_freetalk_opening` 으로 폴백해야 한다.

    ⚠ `target_language` 는 시그니처 대칭용으로만 받는다(다른 seed_* 함수와 같은 모양) —
      이 시드는 한국어 문장이다(문서 원문: "예시 시드 1개, 한국어로"). 자유대화는
      "100% 학습 언어" 규율이지만, 이 한 문장은 **비버가 실제로 하는 말이 아니라 서버가
      대화를 여는 시드**라 이 결정을 그대로 따른다 — 지금은 target_language 를 안 쓴다.
    """
    if not memory_is_substantial(memory):
        return ""
    topic = _pick_recall_topic(memory)
    if not topic:
        return ""
    return _MEMORY_OPENING_TEMPLATE.format(topic=topic)


def _interests_block(interests: list[str] | None) -> str:
    """`[관심사]` 블록 — 실제 회원 관심사가 하나도 없으면 빈 문자열(블록 자체 생략)."""
    real = [i.strip() for i in (interests or []) if isinstance(i, str) and i.strip()]
    if not real:
        return ""
    return "[관심사] " + ", ".join(real)


def _memory_block(memory: dict | None) -> str:
    """`[기억]` 블록 — 기억이 없거나 빈약하면 빈 문자열(블록 자체 생략)."""
    if not memory_is_substantial(memory):
        return ""
    lines = ["[기억 — 지난 자유대화에서 알게 된 것]"]
    summary = (memory.get("summary") or "").strip()
    if summary:
        lines.append(f"- 지금까지 나눈 이야기: {summary}")
    facts = [f for f in (memory.get("facts") or []) if isinstance(f, str) and f.strip()]
    if facts:
        lines.append("- 학습자에 대해 알게 된 것: " + ", ".join(facts[:15]))
    next_topics = [t for t in (memory.get("next_topics") or []) if isinstance(t, str) and t.strip()]
    if next_topics:
        lines.append("- 다음에 물어볼 만한 것: " + ", ".join(next_topics[:5]))
    return "\n".join(lines)


def build_chat_instruction(
    *,
    role: str,
    personality: str,
    level_profile: str,
    locale: str,
    interests: list[str],
    name: str | None = None,
    target_language: str = "한국어",
    locale_label: str | None = None,
    close_tag: str,
    face_rule: str = "",
    language: str = "ko",
    memory: dict | None = None,
) -> str:
    """자유대화 통화의 system_instruction — freetalk D8 기본 대본(`lesson=None`) +
    `[관심사]`(있을 때만) + `[기억]`(있을 때만). `memory` 는
    `chat_memory_service.to_dict(row)` 의 모양(summary/topics/facts/interests/
    next_topics)이다.
    """
    parts = [build_freetalk_instruction(
        role=role, personality=personality, level_profile=level_profile, locale=locale,
        interests=[], name=name, target_language=target_language,
        locale_label=locale_label, close_tag=close_tag, lesson=None,
        face_rule=face_rule, language=language,
    )]
    interests_block = _interests_block(interests)
    if interests_block:
        parts.append(interests_block)
    memory_block = _memory_block(memory)
    if memory_block:
        parts.append(memory_block)
    return "\n\n".join(parts)
