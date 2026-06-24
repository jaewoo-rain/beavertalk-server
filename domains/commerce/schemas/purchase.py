"""구매/결제 DTO."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict


class PurchaseRequest(BaseModel):
    """구매 요청(선택). 카드 정보는 PG 토큰화 후 마스킹값을 프론트가 전달."""

    card_info: Optional[str] = None


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payment_id: int
    payment_date: Optional[datetime]
    price: Optional[Decimal]
    description: Optional[str]


class MemberCharacterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    member_id: int
    character_id: int
    purchase_price: Optional[Decimal]
    purchase_date: Optional[datetime]


class PurchaseResponse(BaseModel):
    """구매 결과 — 소유 레코드 + 결제 레코드 동시 반환."""

    member_character: MemberCharacterOut
    payment: PaymentOut
