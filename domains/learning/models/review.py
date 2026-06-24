"""review (복습) — learning 도메인. sentence 와 N:1 (review_id 대리 PK)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Identity, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.learning.models.sentence import Sentence


class Review(Base, TimestampMixin):
    __tablename__ = "review"

    review_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    sentence_id: Mapped[int] = mapped_column(
        ForeignKey("sentence.sentence_id", ondelete="CASCADE"), index=True, comment="발화",
    )
    record_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="기록 시간")
    voice_url: Mapped[Optional[str]] = mapped_column(Text, comment="보이스 데이터 저장 위치")
    feedback: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON, comment="채점 결과(평가 점수 + 글자별 상/중/하)"
    )

    sentence: Mapped["Sentence"] = relationship(back_populates="reviews")
