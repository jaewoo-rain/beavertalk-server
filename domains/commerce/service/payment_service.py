"""PaymentService — 결제 내역 페이지(이번 달 합계 + 탭별 목록)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from domains.commerce.repository.payment_repository import PaymentRepository
from domains.commerce.schemas.payment import PaymentItem, PaymentPage, PaymentType


class PaymentService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = PaymentRepository(db)

    def get_payments(
        self, member_id: int, type_: PaymentType = "all", page: int = 1, size: int = 10
    ) -> PaymentPage:
        category = None if type_ == "all" else type_  # subscribe/character
        offset = (page - 1) * size
        # has_more 판단 위해 한 개 더 조회
        rows = self.repo.list_by_member(member_id, category, limit=size + 1, offset=offset)
        has_more = len(rows) > size
        items = [PaymentItem.model_validate(p) for p in rows[:size]]

        month_start = datetime.now(timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        month_total = self.repo.month_total(member_id, month_start)

        return PaymentPage(
            month_total=month_total,
            items=items,
            page=page,
            size=size,
            has_more=has_more,
        )
