"""CharacterService — 캐릭터 조회 + 할인가/소유여부 계산(비즈니스 로직).

할인가·소유여부는 모델 컬럼이 아니라 '계산'이므로 서비스 책임.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from domains.commerce.models.character import Character
from domains.commerce.models.discount_event import DiscountEvent
from domains.commerce.repository.character_repository import CharacterRepository
from domains.commerce.repository.member_character_repository import (
    MemberCharacterRepository,
)
from domains.commerce.schemas.character import (
    CharacterDetail,
    CharacterSummary,
    DiscountOut,
    OwnedCharacterOut,
)


def _as_utc(dt: datetime) -> datetime:
    """naive datetime 은 UTC 로 간주(aware 면 그대로).

    Postgres timestamptz 는 aware 로 오지만, sqlite 등은 naive 로 올 수 있어
    aware-vs-naive 비교 오류를 방지한다.
    """
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class CharacterService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.char_repo = CharacterRepository(db)
        self.mc_repo = MemberCharacterRepository(db)

    # ── 조회 ──
    def list_characters(
        self, member_id: int, limit: int = 20, offset: int = 0
    ) -> list[CharacterSummary]:
        characters = self.char_repo.list(limit=limit, offset=offset)
        owned = self.mc_repo.owned_character_ids(member_id)  # 한 번에 소유 id 집합
        return [self._to_summary(c, c.character_id in owned) for c in characters]

    def get_character(self, member_id: int, character_id: int) -> CharacterDetail:
        c = self.char_repo.get(character_id)
        if c is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "캐릭터를 찾을 수 없습니다.")
        owned = self.mc_repo.get(member_id, character_id) is not None
        discount = self.active_discount(c)
        return CharacterDetail(
            character_id=c.character_id,
            name=c.name,
            image_url=c.image_url,
            price=c.price,
            effective_price=discount.discount_price if discount else c.price,
            is_owned=owned,
            description=c.description,
            voice_url=c.voice_url,
            active_discount=DiscountOut.model_validate(discount) if discount else None,
        )

    def list_owned(self, member_id: int) -> list[OwnedCharacterOut]:
        rows = self.mc_repo.list_by_member(member_id)
        return [
            OwnedCharacterOut(
                character_id=mc.character_id,
                name=mc.character.name,
                image_url=mc.character.image_url,
                purchase_price=mc.purchase_price,
                purchase_date=mc.purchase_date,
            )
            for mc in rows
        ]

    # ── 할인 계산(구매 서비스에서도 재사용) ──
    def active_discount(self, character: Character) -> Optional[DiscountEvent]:
        """현재 유효한 할인 행사 1건(활성 + 기간 내). 없으면 None."""
        now = datetime.now(timezone.utc)
        for d in character.discount_events:
            if (
                d.activate
                and d.discount_price is not None
                and d.start_time is not None
                and d.end_time is not None
                and _as_utc(d.start_time) <= now <= _as_utc(d.end_time)
            ):
                return d
        return None

    def effective_price(self, character: Character) -> Decimal:
        d = self.active_discount(character)
        return d.discount_price if d else character.price

    def _to_summary(self, c: Character, owned: bool) -> CharacterSummary:
        return CharacterSummary(
            character_id=c.character_id,
            name=c.name,
            image_url=c.image_url,
            price=c.price,
            effective_price=self.effective_price(c),
            is_owned=owned,
        )
