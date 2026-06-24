"""PaymentRepository — 결제 기록 추가/조회."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domains.commerce.models.payment import Payment


class PaymentRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, payment: Payment) -> Payment:
        self.db.add(payment)
        return payment

    def list_by_member(
        self,
        member_id: int,
        category: Optional[str] = None,  # None=전체, "subscribe"/"character"
        limit: int = 10,
        offset: int = 0,
    ) -> Sequence[Payment]:
        stmt = select(Payment).where(Payment.member_id == member_id)
        if category is not None:
            stmt = stmt.where(Payment.category == category)
        stmt = (
            stmt.order_by(Payment.payment_date.desc().nullslast(), Payment.payment_id.desc())
            .limit(limit)
            .offset(offset)
        )
        return self.db.scalars(stmt).all()

    def month_total(self, member_id: int, since: datetime) -> Decimal:
        """since(이번 달 1일) 이후 결제 총액."""
        stmt = select(func.coalesce(func.sum(Payment.price), 0)).where(
            Payment.member_id == member_id,
            Payment.payment_date >= since,
        )
        return self.db.scalar(stmt) or Decimal("0")
