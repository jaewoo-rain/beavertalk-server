"""alarm (알람) — alarm 도메인."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.account.models.member import Member
    from domains.alarm.models.schedule import Schedule
    from domains.commerce.models.character import Character


class Alarm(Base, TimestampMixin):
    __tablename__ = "alarm"

    alarm_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE"), index=True, comment="회원",
    )
    character_id: Mapped[int] = mapped_column(
        ForeignKey("character.character_id", ondelete="RESTRICT"), index=True, comment="캐릭터",
    )
    time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="알람 시간")
    is_activate: Mapped[Optional[bool]] = mapped_column(Boolean, comment="활성화 상태")

    member: Mapped["Member"] = relationship(back_populates="alarms")
    character: Mapped["Character"] = relationship(lazy="select")  # 단방향(필요 시 쿼리에서 joinedload)
    schedules: Mapped[list["Schedule"]] = relationship(
        back_populates="alarm", cascade="all, delete-orphan",
        passive_deletes=True, lazy="selectin",
    )
