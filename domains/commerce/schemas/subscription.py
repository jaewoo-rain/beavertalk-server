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
    # 3티어 재편: 기본값 pro 라 기존 호출은 그대로 동작한다(하위호환).
    # ⚠ 이 API 는 IAP 전환 시 폐기된다 — 여기 plan 은 결제 미연동 기간의 임시 통로다.
    plan: Literal["pro", "max"] = "pro"
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
      - state: 결제 관계가 어디 있나(8종)
      - plan : 어떤 기능 묶음이 열리나(pro | max)
    grace·on_hold·ending 은 "직전에 무슨 플랜이었는지"를 유지하므로 state 만으로는
    플랜을 알 수 없다. 그래서 plan 을 따로 싣는다.

    ⛔ plan 은 free·expired 를 뺀 **전 상태에서 반드시 채운다.** 빠지면 앱이
       isPlanInferred=true 로 Pro 라고 가정한다 — Max 회원이 결제에 실패하면
       Pro 화면(잘못된 해지 안내)을 보게 된다.
    """

    state: Literal[
        "free", "trial", "active_pro", "active_max",
        "grace", "on_hold", "ending", "expired",
    ]
    plan: Optional[Literal["pro", "max"]] = None
    subscribe_id: Optional[int] = None
    price: Optional[Decimal] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    retrying_until: Optional[datetime] = None
    paused_since: Optional[datetime] = None
