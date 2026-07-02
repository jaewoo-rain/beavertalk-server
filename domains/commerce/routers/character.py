"""commerce 라우터 — 캐릭터 목록/상세/구매 + 내 소유 캐릭터."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from core.deps import CurrentMember, DbSession, PageParams
from domains.commerce.schemas.character import (
    CharacterDetail,
    CharacterSummary,
    OwnedCharacterOut,
)
from domains.commerce.schemas.purchase import PurchaseRequest, PurchaseResponse
from domains.commerce.service.character_service import CharacterService
from domains.commerce.service.purchase_service import PurchaseService

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


@router.post(
    "/characters/{character_id}/purchase",
    response_model=PurchaseResponse,
    status_code=status.HTTP_201_CREATED,
)
def purchase_character(
    character_id: int,
    member: CurrentMember,
    db: DbSession,
    data: PurchaseRequest | None = None,
) -> PurchaseResponse:
    """캐릭터 구매 — 결제 후 보유 처리. 이미 보유한 캐릭터면 중복구매가 막힌다."""
    card_info = data.card_info if data else None
    return PurchaseService(db).purchase(member.member_id, character_id, card_info)


@router.get("/members/me/characters", response_model=list[OwnedCharacterOut])
def my_characters(member: CurrentMember, db: DbSession) -> list[OwnedCharacterOut]:
    """내가 보유한(구매한) 캐릭터 목록 — 구매가·구매일 포함."""
    return CharacterService(db).list_owned(member.member_id)

# todo: 이벤트 중인 캐릭터들 가격 및 정보 조회 
