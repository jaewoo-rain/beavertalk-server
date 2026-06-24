"""discount_event (할인 행사) — commerce 도메인. character 와 N:1."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, Numeric
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.commerce.models.character import Character


class DiscountEvent(Base, TimestampMixin):
    __tablename__ = "discount_event"

    discount_event_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    character_id: Mapped[int] = mapped_column(
        ForeignKey("character.character_id", ondelete="CASCADE"),
        index=True, comment="대상 캐릭터",
    )
    discount_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), comment="할인된 가격")
    start_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="시작 시간")
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="끝나는 시간")
    activate: Mapped[Optional[bool]] = mapped_column(Boolean, comment="활성화 여부")

    character: Mapped["Character"] = relationship(back_populates="discount_events")
