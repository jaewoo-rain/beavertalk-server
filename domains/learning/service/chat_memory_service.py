"""chat_memory_service — 자유대화 기억 저장소(C6, 2026-09-23).

docs/plans/2026-09-22-프리미엄-자유대화-15분-달력.md 참조. C7(자유대화 통화 세션)이
통화 시작 시 `load`, 통화 끝(기억 추출 뒤)에 `merge` 를 부른다 — 이 파일은 그 배선을
모른다(테이블·순수 병합 로직만).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import gemini_analysis
from domains.learning.models.chat_memory import ChatMemory
from domains.learning.repository.chat_memory_repository import ChatMemoryRepository

logger = logging.getLogger(__name__)

# 문서 C6 그대로 — 배열 상한(코드가 지킨다, DB 제약 아님).
SUMMARY_CHAR_CAP = 1200
TOPICS_CAP = 10
FACTS_CAP = 15
INTERESTS_CAP = 10
NEXT_TOPICS_CAP = 5

# ⛔ LLM 생성 0 규율(persona_prompt 와 같은 원칙)은 이 지시문엔 적용되지 않는다 —
#   이건 통화 프롬프트가 아니라 **재압축 도구 호출**이다(사용자에게 안 보임).
_RECOMPRESS_INSTRUCTION = """너는 두 요약을 하나로 합치는 도구다.
[옛 요약]은 이전 자유대화들의 누적 요약이고 [이번 통화 요약]은 방금 끝난 통화의 요약이다.
둘을 하나의 자연스러운 요약으로 합쳐라. 규칙:
- 최종 요약은 1200자를 넘지 않는다.
- 오래되고 덜 중요한 내용은 줄이고, 최근 정보를 우선한다.
- 사실을 추가하거나 지어내지 않는다 — 두 요약에 있는 내용만 사용한다.
- summary 칸 하나만 채운다."""


class _RecompressOut(BaseModel):
    summary: str = ""


def load(db: Session, member_id: int, language: str) -> ChatMemory | None:
    """이 회원·언어의 자유대화 기억 — 없으면 None(그 언어로는 처음 자유대화)."""
    return ChatMemoryRepository(db).get(member_id, language)


def to_dict(row: ChatMemory | None) -> dict | None:
    """ChatMemory 행 → 프롬프트 조립(core/prompts/chat.py)이 쓰는 평범한 dict. 행이 없으면 None."""
    if row is None:
        return None
    return {
        "summary": row.summary or "",
        "topics": list(row.topics or []),
        "facts": list(row.facts or []),
        "interests": list(row.interests or []),
        "next_topics": list(row.next_topics or []),
    }


def _merge_capped(old: list | None, new: list | None, cap: int) -> list[str]:
    """새 항목을 **앞에**(최신 우선) 두고 중복 제거한 뒤 상한을 자른다.

    ⚠ `dict.fromkeys` 는 이 코드베이스의 순서보존 dedup 관용구다(normalcall_service
      의 이력 조립·member_service._validate_reasons 와 같은 패턴). 앞에 둔 새 항목이
      중복이면 그 자리(최신 위치)를 지키고, 뒤의 옛 중복은 버려진다.
    """
    combined = list(new or []) + list(old or [])
    deduped = list(dict.fromkeys(
        s.strip() for s in combined if isinstance(s, str) and s.strip()
    ))
    return deduped[:cap]


def _fallback_summary(old_summary: str, new_summary: str) -> str:
    """재압축을 못 하면(LLM 없음·실패) 옛 요약을 유지하고 이번 요약을 뒤에 붙여 자른다."""
    combined = f"{old_summary} {new_summary}".strip()
    return combined[:SUMMARY_CHAR_CAP]


async def _recompress_summary(client, model: str | None, old_summary: str, new_summary: str) -> str:
    """옛 요약 + 이번 통화 요약 → 1,200자 이내로 재압축(JUDGE_MODEL).

    ⛔ 실패해도 기억 저장 자체를 막지 않는다(R5) — LLM 미제공(client=None, 시험·
      graceful degradation)·호출 실패·빈 응답이면 전부 `_fallback_summary` 로 떨어진다.
    """
    if old_summary == "":
        return new_summary.strip()[:SUMMARY_CHAR_CAP]
    if not client or not model:
        return _fallback_summary(old_summary, new_summary)
    try:
        out = await gemini_analysis.generate_structured(
            client, model,
            system_instruction=_RECOMPRESS_INSTRUCTION,
            prompt=f"[옛 요약]\n{old_summary}\n\n[이번 통화 요약]\n{new_summary}",
            schema=_RecompressOut, temperature=0.2, thinking_budget=0,
        )
    except Exception as exc:  # noqa: BLE001 — 재압축 실패가 기억 저장을 죽이면 안 된다(R5)
        logger.warning("chat_memory 재압축 호출 실패(폴백): %s", exc)
        return _fallback_summary(old_summary, new_summary)
    if out is None or not (out.summary or "").strip():
        return _fallback_summary(old_summary, new_summary)
    return out.summary.strip()[:SUMMARY_CHAR_CAP]


async def merge(
    db: Session, member_id: int, language: str, call_id: int, slots: dict,
    *, client=None, model: str | None = None,
) -> ChatMemory:
    """이번 통화의 슬롯을 기존 기억에 합친다.

    slots(전부 선택, 없으면 빈 값 취급):
        summary: str        — 이번 통화의 짧은 요약(재압축 입력)
        topics: list[str]   — 이번 통화에서 나온 새 화제
        facts: list[str]    — 새로 알게 된 사실
        interests: list[str]— 드러난 관심사
        next_topics: list[str] — 다음에 이어 말할 거리

    ⭐⭐ 멱등: `last_call_id` 가 이미 이 `call_id` 면 아무것도 안 하고 그대로 돌려준다
      — 통화후 파이프라인은 fire-and-forget 이라 재시도·중복 호출이 있을 수 있다.
    client/model: 재압축용(core/gemini_analysis.generate_structured). 없으면
      `_fallback_summary` 로 떨어진다(시험·LLM 미가용 환경에서도 저장은 된다).
    """
    repo = ChatMemoryRepository(db)
    row = repo.get(member_id, language)
    if row is not None and row.last_call_id == call_id:
        return row

    old_summary = row.summary if row is not None else ""
    new_summary = (slots.get("summary") or "").strip()
    summary = await _recompress_summary(client, model, old_summary, new_summary)

    topics = _merge_capped(row.topics if row is not None else [], slots.get("topics"), TOPICS_CAP)
    facts = _merge_capped(row.facts if row is not None else [], slots.get("facts"), FACTS_CAP)
    interests = _merge_capped(
        row.interests if row is not None else [], slots.get("interests"), INTERESTS_CAP
    )
    next_topics = _merge_capped(
        row.next_topics if row is not None else [], slots.get("next_topics"), NEXT_TOPICS_CAP
    )

    if row is None:
        row = ChatMemory(member_id=member_id, language=language)
        repo.add(row)
    row.summary = summary
    row.topics = topics
    row.facts = facts
    row.interests = interests
    row.next_topics = next_topics
    row.last_call_id = call_id
    db.commit()
    db.refresh(row)
    return row
