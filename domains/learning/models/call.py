"""call (전화/통화) — learning 도메인."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.account.models.member import Member
    from domains.commerce.models.character import Character
    from domains.learning.models.call_raw_data import CallRawData
    from domains.learning.models.sentence import Sentence


class Call(Base, TimestampMixin):
    __tablename__ = "call"
    __table_args__ = (
        Index("ix_call_member_date", "member_id", "call_date"),  # 내 통화 최신순
    )

    call_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE"), index=True, comment="회원",
    )
    character_id: Mapped[int] = mapped_column(
        ForeignKey("character.character_id", ondelete="RESTRICT"), index=True, comment="캐릭터",
    )
    call_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="전화 날짜")
    total_time: Mapped[Optional[int]] = mapped_column(Integer, comment="총 통화 시간(초)")
    summary: Mapped[Optional[str]] = mapped_column(Text, comment="대화 내용 한 줄 요약")
    rating: Mapped[Optional[int]] = mapped_column(Integer, comment="만족도(1~3점)")
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'ongoing'"),
        comment="분석 상태(ongoing/analyzing/done/failed)",
    )
    mode: Mapped[Optional[str]] = mapped_column(
        Text, comment="감지된 통화 모드(conversation/study/unknown)",
    )

    member: Mapped["Member"] = relationship(back_populates="calls")
    character: Mapped["Character"] = relationship(lazy="select")  # 단방향(필요 시 쿼리에서 joinedload)
    raw_data: Mapped[list["CallRawData"]] = relationship(
        back_populates="call", cascade="all, delete-orphan", passive_deletes=True,
    )
    sentences: Mapped[list["Sentence"]] = relationship(
        back_populates="call", cascade="all, delete-orphan", passive_deletes=True,
    )
