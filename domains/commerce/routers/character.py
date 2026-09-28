"""commerce 라우터 — 캐릭터 목록/상세 + 내 소유 캐릭터.

⛔ 구매 API(POST /characters/{id}/purchase)는 **삭제했다**(2026-09-29 사장님 지시) — 돈을
받지 않고 지급하던 테스트 경로였다(운영 유료 보유 24건 중 영수증 1건). 무료 캐릭터는
처음부터 보유(파생, entitlements.is_free_character)이고, 유료는 스토어 영수증
(POST /purchases/verify)으로만 지급한다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from core.deps import CurrentMember, DbSession, PageParams
from domains.commerce.schemas.character import (
    CharacterDetail,
    CharacterSummary,
    OwnedCharacterOut,
)
from domains.commerce.service.character_service import CharacterService

router = APIRouter(tags=["commerce"])


@router.get("/characters", response_model=list[CharacterSummary])
def list_characters(
    member: CurrentMember, db: DbSession, page: PageParams = Depends()
) -> list[CharacterSummary]:
    """캐릭터 상점 목록 — 이름·가격·보유 여부 요약, 페이지네이션."""
    return CharacterService(db).list_characters(member.member_id, page.limit, page.offset)


@router.get("/characters/{character_id}", response_model=CharacterDetail)
def get_character(
    character_id: int, member: CurrentMember, db: DbSession
) -> CharacterDetail:
    """캐릭터 상세 — 음성·설명·가격·보유 여부(없는 캐릭터면 404)."""
    return CharacterService(db).get_character(member.member_id, character_id)


@router.get("/members/me/characters", response_model=list[OwnedCharacterOut])
def my_characters(member: CurrentMember, db: DbSession) -> list[OwnedCharacterOut]:
    """내가 보유한 캐릭터 목록 — 구매한 것 + 0원 캐릭터(처음부터 보유). 구매가·구매일 포함."""
    return CharacterService(db).list_owned(member.member_id)

# todo: 이벤트 중인 캐릭터들 가격 및 정보 조회 
