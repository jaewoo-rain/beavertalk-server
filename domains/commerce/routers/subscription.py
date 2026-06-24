"""subscription 라우터 — 구독 시작/목록/취소."""

from __future__ import annotations

from fastapi import APIRouter, status

from core.deps import CurrentMember, DbSession
from domains.commerce.schemas.subscription import SubscribeCreate, SubscriptionOut
from domains.commerce.service.subscription_service import SubscriptionService

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.post("", response_model=SubscriptionOut, status_code=status.HTTP_201_CREATED)
def start_subscription(
    data: SubscribeCreate, member: CurrentMember, db: DbSession
) -> SubscriptionOut:
    return SubscriptionService(db).start(member.member_id, data)


@router.get("", response_model=list[SubscriptionOut])
def list_subscriptions(member: CurrentMember, db: DbSession) -> list[SubscriptionOut]:
    return SubscriptionService(db).list(member.member_id)


@router.post("/{subscribe_id}/cancel", response_model=SubscriptionOut)
def cancel_subscription(
    subscribe_id: int, member: CurrentMember, db: DbSession
) -> SubscriptionOut:
    return SubscriptionService(db).cancel(member.member_id, subscribe_id)
