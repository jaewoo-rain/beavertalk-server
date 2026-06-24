"""schedule (반복 요일) — alarm 도메인. alarm 과 1:N."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, ForeignKey, Identity, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.alarm.models.alarm import Alarm


class Schedule(Base, TimestampMixin):
    __tablename__ = "schedule"

    schedule_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    alarm_id: Mapped[int] = mapped_column(
        ForeignKey("alarm.alarm_id", ondelete="CASCADE"), index=True, comment="알람",
    )
    day_of_week: Mapped[Optional[str]] = mapped_column(Text, comment="요일")

    alarm: Mapped["Alarm"] = relationship(back_populates="schedules")
