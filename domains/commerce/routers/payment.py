"""payment 라우터 — 결제 내역 페이지(이번 달 합계 + 전체/구독/캐릭터 탭)."""

from __future__ import annotations

from fastapi import APIRouter, Query

from core.deps import CurrentMember, DbSession
from domains.commerce.schemas.payment import PaymentPage, PaymentType
from domains.commerce.service.payment_service import PaymentService

router = APIRouter(prefix="/payments", tags=["payments"])


@router.get("", response_model=PaymentPage)
def list_payments(
    member: CurrentMember,
    db: DbSession,
    type: PaymentType = Query("all", description="all=전체, subscribe=구독, character=캐릭터"),
    page: int = Query(1, ge=1),
) -> PaymentPage:
    """탭(type) 하나로 전체/구독/캐릭터 전환. 페이지당 10개 + 이번 달 합계."""
    return PaymentService(db).get_payments(member.member_id, type, page, size=10)
