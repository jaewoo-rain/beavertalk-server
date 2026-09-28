"""해지 사유 DTO — §17(2026-09-28)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict


class ChurnReasonIn(BaseModel):
    """`POST /members/me/churn-reasons` 입력. member_id 는 토큰에서 온다(회원이 못 정한다)."""

    reason: Literal["expensive", "unused", "missing", "other_app", "other"]
    subscribe_id: int
    # 서버가 reason=="expensive" 로 추론하지 않는다 — 오퍼 시트가 실제로 떴는지는
    # 앱만 안다(할인 이벤트가 없어 안 떴을 수도 있다).
    offer_shown: bool


class ChurnReasonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    churn_reason_id: int
    subscribe_id: Optional[int]
    reason: str
    offer_shown: bool
    end_date_snapshot: Optional[datetime]
