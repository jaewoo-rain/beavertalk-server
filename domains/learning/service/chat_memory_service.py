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
    """재압축을 못 하면(LLM 없음·실패) **최신 내용을 우선** 유지하고 옛 요약을 뒤에 붙여 자른다.

    ⛔⛔ Q3(2026-09-24, 프론트/운영 실측) — 예전엔 옛 요약을 앞에 두고 이번 요약을
    뒤에 붙여 잘랐다. `SUMMARY_CHAR_CAP`(1,200자)·통화당 조각 100~250자라 5~8통만
    지나도 캡에 닿고, 그 뒤로는 **새 통화 내용이 한 글자도 안 들어갔다**(오래 쓴
    회원일수록 비버가 최근 대화를 통째로 모르는 결과 — old 가 이미 캡을 채우면
    new 를 아무리 붙여도 잘려 나갔다). 재압축(LLM)이 정상이면 최신·과거를 가려
    재구성하므로 문제가 없었지만, `merge`(정확히는 옛 `normalcall_service._do_merge`)
    가 threadpool 안에서 `asyncio.run` 으로 이벤트 루프를 새로 만들어 공유 genai
    클라이언트를 쓰던 결함 때문에 재압축이 **항상** 실패해 이 폴백이 사실상 상시
    경로였다(고친 곳: normalcall_service.extract_and_merge_chat_memory).
    ⇒ 새 요약을 앞에 두고 옛 요약을 뒤에 붙여 자른다 — 잘리는 쪽은 이제 **옛 내용**이다.
    """
    combined = f"{new_summary} {old_summary}".strip()
    return combined[:SUMMARY_CHAR_CAP]


async def recompress_summary(client, model: str | None, old_summary: str, new_summary: str) -> str:
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


def load_old_summary_for_merge(
    db: Session, member_id: int, language: str, call_id: int
) -> str | None:
    """멱등 게이트 + 재압축 입력(old_summary) 조회 — **순수 동기 DB 읽기**.

    ⭐⭐ 멱등: `last_call_id` 가 이미 이 `call_id` 면 **None** 을 돌려준다(호출부가
    이걸 보고 나머지 단계를 건너뛴다) — 통화후 파이프라인은 fire-and-forget 이라
    재시도·중복 호출이 있을 수 있다. None 은 "아직 요약이 없다"(빈 문자열)와 값이
    겹치지 않게 하는 신호값이다.
    ⛔⛔ Q3(2026-09-24) — 이 함수와 `merge_sync` 사이(재압축·LLM 호출)를 **메인
    이벤트 루프**에서 실행하라고 이렇게 나눴다. `recompress_summary` 참조.
    """
    row = ChatMemoryRepository(db).get(member_id, language)
    if row is not None and row.last_call_id == call_id:
        return None
    return row.summary if row is not None else ""


def merge_sync(
    db: Session, member_id: int, language: str, call_id: int, slots: dict, summary: str,
) -> ChatMemory:
    """DB 병합만 수행하는 **순수 동기** 함수 — `summary` 는 이미 재압축이 끝난 최종값.

    slots(전부 선택, 없으면 빈 값 취급):
        topics/facts/interests/next_topics: list[str]

    ⛔⛔ Q3(2026-09-24, 프론트 실기기 QA — 자유대화 「기억」 요약이 구조적으로 항상
    실패) — 예전엔 이 병합 전체가 `async def merge`(LLM 재압축 포함) 하나였고,
    그걸 `normalcall_service._do_merge` 가 **threadpool(run_db) 안에서
    `asyncio.run()`으로 새 이벤트 루프를 만들어** 돌렸다. `client`(lifespan 이 만든
    공유 genai 클라이언트, 즉 공유 httpx.AsyncClient)는 **메인 루프에 바인딩**돼
    있어서, 새 루프에서 쓰면 `RuntimeError: ... bound to a different event loop`
    가 났고, `gemini_analysis` 의 포괄 except 가 그걸 삼켜 `recompress_summary` 가
    **항상** `_fallback_summary` 로 떨어지고 있었다(운영에서 상시 재현 — 에이전트가
    로컬 재현, bt-back 이 코드로 확인). LLM 이 없는 이 함수를 만든 이유는 그 함정
    자체를 구조적으로 없애기 위함이다 — 재압축은 호출부가 메인 루프에서 미리 끝내고
    (`recompress_summary`), 여기는 결과 문자열만 받아 순수 DB 쓰기만 한다.
    ⚠ 멱등 재확인 — 호출부(`load_old_summary_for_merge`)가 이미 게이트를 통과시켰지만,
    두 단계 사이 레이스에 대비해 여기서도 `last_call_id` 를 다시 본다(값싼 재확인).
    """
    repo = ChatMemoryRepository(db)
    row = repo.get(member_id, language)
    if row is not None and row.last_call_id == call_id:
        return row

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


async def merge(
    db: Session, member_id: int, language: str, call_id: int, slots: dict,
    *, client=None, model: str | None = None,
) -> ChatMemory:
    """이번 통화의 슬롯을 기존 기억에 합친다 — `load_old_summary_for_merge` →
    `recompress_summary` → `merge_sync` 를 한 호출로 이어 붙인 편의 함수.

    slots(전부 선택, 없으면 빈 값 취급):
        summary: str        — 이번 통화의 짧은 요약(재압축 입력)
        topics: list[str]   — 이번 통화에서 나온 새 화제
        facts: list[str]    — 새로 알게 된 사실
        interests: list[str]— 드러난 관심사
        next_topics: list[str] — 다음에 이어 말할 거리

    ⛔⛔ Q3(2026-09-24) — **단일 이벤트 루프**에서 db·client 를 함께 쓸 수 있을 때만
    이 함수를 써라(이 파일의 단위시험이 그 경우다 — pytest-asyncio 가 db 세션과
    같은 루프에서 그냥 await 한다). `normalcall_service.extract_and_merge_chat_memory`
    처럼 db 접근을 threadpool(run_db)로 오프로드해야 하는 자리에서는 이 함수를
    통째로 부르지 말고 세 단계를 쪼개 불러라(재압축만 메인 루프, 나머지는 threadpool)
    — 안 그러면 옛 `asyncio.run` 함정이 그대로 재발한다.
    client/model: 재압축용(core/gemini_analysis.generate_structured). 없으면
      `_fallback_summary` 로 떨어진다(시험·LLM 미가용 환경에서도 저장은 된다).
    """
    old_summary = load_old_summary_for_merge(db, member_id, language, call_id)
    if old_summary is None:
        row = ChatMemoryRepository(db).get(member_id, language)
        assert row is not None  # load_old_summary_for_merge 가 None 을 준 건 row 가 있다는 뜻
        return row

    new_summary = (slots.get("summary") or "").strip()
    summary = await recompress_summary(client, model, old_summary, new_summary)
    return merge_sync(db, member_id, language, call_id, slots, summary)
