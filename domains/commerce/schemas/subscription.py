"""구독 DTO."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class SubscribeCreate(BaseModel):
    price: Decimal = Field(gt=0)  # 음수/0 금액 차단(서버 요금제 도입 전 최소 방어)
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    card_info: Optional[str] = None
    # 2단화(D1): free 없음(행이 없으면 free) · premium 한 값뿐.
    # ⚠ 이 API 는 IAP 전환 시 폐기된다 — 여기 plan 은 결제 미연동 기간의 임시 통로다.
    plan: Literal["premium"] = "premium"
    billing_period: Optional[Literal["monthly", "yearly"]] = None


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    subscribe_id: int
    start_date: Optional[datetime]
    end_date: Optional[datetime]
    price: Optional[Decimal]
    is_activate: Optional[bool]


class SubscriptionStatusOut(BaseModel):
    """회원 단위 **현재 구독 상태** 1건 — `GET /subscriptions/status`.

    ⚠ 이 필드 집합은 **앱과의 계약**이다(flutter `SubscriptionStatusDto`).
       키를 바꾸면 앱이 파싱을 거부하고 구식 목록 추론으로 폴백한다.

    두 축을 따로 내린다:
      - state: 결제 관계가 어디 있나(7종, D1 2단화로 active_pro/active_max 가
        active_premium 하나로 합쳐졌다)
      - plan : 어떤 기능 묶음이 열리나(premium 하나뿐 — free 는 plan=None)
    grace·on_hold·ending 은 "직전에 무슨 플랜이었는지"를 유지하므로 state 만으로는
    플랜을 알 수 없다. 그래서 plan 을 따로 싣는다.

    ⛔ plan 은 free·expired 를 뺀 **전 상태에서 반드시 채운다.** 빠지면 앱이
       isPlanInferred=true 로 잘못 가정할 수 있다.
    """

    state: Literal[
        "free", "trial", "active_premium",
        "grace", "on_hold", "ending", "expired",
    ]
    plan: Optional[Literal["premium"]] = None
    subscribe_id: Optional[int] = None
    price: Optional[Decimal] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    retrying_until: Optional[datetime] = None
    paused_since: Optional[datetime] = None
