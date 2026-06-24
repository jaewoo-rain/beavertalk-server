"""구독 DTO."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class SubscribeCreate(BaseModel):
    price: Decimal = Field(gt=0)  # 음수/0 금액 차단(서버 요금제 도입 전 최소 방어)
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    card_info: Optional[str] = None


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    subscribe_id: int
    start_date: Optional[datetime]
    end_date: Optional[datetime]
    price: Optional[Decimal]
    is_activate: Optional[bool]
