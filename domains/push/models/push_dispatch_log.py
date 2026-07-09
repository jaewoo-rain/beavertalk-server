"""push_dispatch_log (발송 멱등 로그) — push 도메인.

예약전화 디스패처가 (alarm, 의도된 벽분 버킷) 단위로 UNIQUE INSERT 를 시도해
중복 발송을 막는 멱등 클레임 테이블. 오래된 로그는 주기적으로 purge 한다.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class PushDispatchLog(Base, TimestampMixin):
    __tablename__ = "push_dispatch_log"

    push_dispatch_log_id: Mapped[int] = mapped_column(
        BigInteger, Identity(), primary_key=True
    )
    alarm_id: Mapped[int] = mapped_column(
        ForeignKey("alarm.alarm_id", ondelete="CASCADE"), index=True, comment="알람",
    )
    intended_fire_minute: Mapped[str] = mapped_column(
        Text, nullable=False, comment="의도된 벽분 버킷, 예 2026-07-06 08:00",
    )

    __table_args__ = (
        UniqueConstraint(
            "alarm_id", "intended_fire_minute", name="uq_push_dispatch_alarm_minute",
        ),
        Index("ix_push_dispatch_log_created_at", "created_at"),
    )
