"""alarm (알람) — alarm 도메인."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, Text
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
    # ⭐ 프론트 요청 #1(2026-09-23) — 알람에서 건 통화의 종류. FCM 페이로드엔 알람 id 가
    #   없어 앱은 어느 알람인지 되짚을 수 없다(캐릭터도 같은 이유로 서버가 정한다,
    #   normalcall_service.resolve_call_character). start.call_type 이 같이 와도 이 값이
    #   이긴다 — 안 그러면 알람 설정이 안 먹는다(call_session.py 라우팅 참조).
    call_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="auto",
        comment="알람 통화 종류 — auto(학습) · chat(자유대화)",
    )

    member: Mapped["Member"] = relationship(back_populates="alarms")
    character: Mapped["Character"] = relationship(lazy="select")  # 단방향(필요 시 쿼리에서 joinedload)
    schedules: Mapped[list["Schedule"]] = relationship(
        back_populates="alarm", cascade="all, delete-orphan",
        passive_deletes=True, lazy="selectin",
    )
