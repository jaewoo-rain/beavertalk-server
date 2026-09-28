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
        """since(이번 달 1일) 이후 결제 총액.

        ⭐⭐ §22-⑥(2026-09-28) — 샌드박스·스텁 결제는 집계에서 뺀다(iap_receipt.
        is_sandbox/is_stub 과 같은 규율: 저장은 하되 — list_by_member 는 그대로
        보여준다 — 운영 집계에서만 제외한다). 안 빼면 테스터의 $0 짜리 테스트
        결제가 "이번 달 결제"에 실제 매출처럼 섞여 든다.
        """
        stmt = select(func.coalesce(func.sum(Payment.price), 0)).where(
            Payment.member_id == member_id,
            Payment.payment_date >= since,
            Payment.is_sandbox.is_(False),
            Payment.is_stub.is_(False),
        )
        return self.db.scalar(stmt) or Decimal("0")
