"""character 관련 DTO."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict


class DiscountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    discount_price: Optional[Decimal]
    start_time: Optional[datetime]
    end_time: Optional[datetime]


class CharacterSummary(BaseModel):
    """목록용 — 얕게. prompt/voice_url 제외."""

    character_id: int
    name: str
    image_url: Optional[str]
    price: Decimal
    effective_price: Decimal  # 활성 할인 반영가(서버 계산)
    is_owned: bool            # 현재 회원 소유 여부


class CharacterDetail(CharacterSummary):
    """상세용 — 깊게. prompt 는 내부용이라 제외."""

    description: Optional[str]
    voice_url: Optional[str]
    active_discount: Optional[DiscountOut]


class OwnedCharacterOut(BaseModel):
    """내 소유 캐릭터 1건(구매 정보 평탄화)."""

    character_id: int
    name: str
    image_url: Optional[str]
    purchase_price: Optional[Decimal]
    purchase_date: Optional[datetime]
