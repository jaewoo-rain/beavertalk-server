"""subscription 라우터 — 구독 시작/목록/취소."""

from __future__ import annotations

from fastapi import APIRouter, status

from core.deps import CurrentMember, DbSession
from domains.commerce.schemas.subscription import (
    SubscribeCreate,
    SubscriptionOut,
    SubscriptionStatusOut,
)
from domains.commerce.service.subscription_service import SubscriptionService

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.post("", response_model=SubscriptionOut, status_code=status.HTTP_201_CREATED)
def start_subscription(
    data: SubscribeCreate, member: CurrentMember, db: DbSession
) -> SubscriptionOut:
    """구독 시작 — 결제 후 구독을 활성화(기간·금액 저장)한다."""
    return SubscriptionService(db).start(member.member_id, data)


@router.get("", response_model=list[SubscriptionOut])
def list_subscriptions(member: CurrentMember, db: DbSession) -> list[SubscriptionOut]:
    """내 구독 목록(활성/만료 포함)."""
    return SubscriptionService(db).list(member.member_id)


@router.get("/status", response_model=SubscriptionStatusOut)
def get_subscription_status(
    member: CurrentMember, db: DbSession
) -> SubscriptionStatusOut:
    """내 **현재 구독 상태** 1건 — 상태 8종 + 플랜.

    목록(`GET /subscriptions`)이 행 이력이라면 이건 "지금 어디에 있나"의 단일 답이다.
    앱이 행 목록에서 상태를 역추론하면 해지 안내가 틀어지므로 판정을 서버가 소유한다.

    ⚠ 라우트 순서: `/{subscribe_id}` 형태의 경로 매개변수 라우트보다 **위**에 있어야
      "status" 가 id 로 먹히지 않는다(지금은 그런 라우트가 없지만 추가될 때를 대비).
    """
    return SubscriptionService(db).status(member.member_id)


@router.post("/{subscribe_id}/cancel", response_model=SubscriptionOut)
def cancel_subscription(
    subscribe_id: int, member: CurrentMember, db: DbSession
) -> SubscriptionOut:
    """구독 취소(해지) — 해당 구독을 비활성화한다."""
    return SubscriptionService(db).cancel(member.member_id, subscribe_id)
