"""결제 내역 페이지 DTO."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

# 결제 분류
PaymentCategory = Literal["subscribe", "character"]
# 목록 탭 필터(전체/구독/캐릭터)
PaymentType = Literal["all", "subscribe", "character"]


class PaymentItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payment_id: int
    payment_date: Optional[datetime]
    description: Optional[str]
    card_info: Optional[str]
    price: Optional[Decimal]
    category: Optional[str]


class PaymentPage(BaseModel):
    """결제 페이지 — 이번 달 합계 + 내역(페이징)."""

    month_total: Decimal       # 이번 달 결제 총액
    items: list[PaymentItem]
    page: int
    size: int
    has_more: bool
