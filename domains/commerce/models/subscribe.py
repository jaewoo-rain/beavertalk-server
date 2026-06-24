"""subscribe (구독) — commerce 도메인. member 와 N:1."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, Numeric
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.account.models.member import Member


class Subscribe(Base, TimestampMixin):
    __tablename__ = "subscribe"

    subscribe_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE"), index=True, comment="회원",
    )
    start_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="시작(결제) 날짜")
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="끝나는 날짜")
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), comment="결제 금액")
    is_activate: Mapped[Optional[bool]] = mapped_column(Boolean, comment="활성화 여부")

    member: Mapped["Member"] = relationship(back_populates="subscribes")
