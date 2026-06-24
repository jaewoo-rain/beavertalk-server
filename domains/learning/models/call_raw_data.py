"""call_raw_data (전화 원본 음성 데이터) — learning 도메인. call 과 1:N."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, ForeignKey, Identity, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.learning.models.call import Call


class CallRawData(Base, TimestampMixin):
    __tablename__ = "call_raw_data"

    call_raw_data_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    call_id: Mapped[int] = mapped_column(
        ForeignKey("call.call_id", ondelete="CASCADE"), index=True, comment="통화",
    )
    content: Mapped[Optional[str]] = mapped_column(Text, comment="음성 데이터 전사")
    voice_url: Mapped[Optional[str]] = mapped_column(Text, comment="보이스 데이터 저장 위치")
    total_time: Mapped[Optional[int]] = mapped_column(Integer, comment="음성 시간(초)")

    call: Mapped["Call"] = relationship(back_populates="raw_data")
