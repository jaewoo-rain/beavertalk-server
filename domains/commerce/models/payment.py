"""payment (결제) — commerce 도메인. member 와 N:1."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.account.models.member import Member


class Payment(Base, TimestampMixin):
    __tablename__ = "payment"

    payment_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE"), index=True, comment="회원",
    )
    payment_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="결제 날짜")
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), comment="결제 금액")
    description: Mapped[Optional[str]] = mapped_column(Text, comment="결제 내용")
    category: Mapped[Optional[str]] = mapped_column(
        Text, index=True, comment="결제 분류(subscribe/character)"
    )
    card_info: Mapped[Optional[str]] = mapped_column(Text, comment="카드 정보(마스킹)")

    member: Mapped["Member"] = relationship(back_populates="payments")
