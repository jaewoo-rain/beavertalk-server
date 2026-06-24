"""sentence (발화) — learning 도메인.

call 의 자식(N:1), evaluation 의 부모(1:1), review 의 부모(1:N).
deleted_at 으로 소프트 삭제(개별 문장 삭제는 하드 삭제하지 않음).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.learning.models.call import Call
    from domains.learning.models.evaluation import Evaluation
    from domains.learning.models.review import Review


class Sentence(Base, TimestampMixin):
    __tablename__ = "sentence"

    sentence_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    call_id: Mapped[int] = mapped_column(
        ForeignKey("call.call_id", ondelete="CASCADE"), index=True, comment="통화",
    )
    korean_sentence: Mapped[Optional[str]] = mapped_column(Text, comment="한국어 문장")
    native_sentence: Mapped[Optional[str]] = mapped_column(Text, comment="모국어 문장")
    locale: Mapped[Optional[str]] = mapped_column(Text, comment="언어")
    voice_url: Mapped[Optional[str]] = mapped_column(Text, comment="보이스 데이터 저장 위치")
    is_bookmarked: Mapped[Optional[bool]] = mapped_column(Boolean, comment="북마크 여부")
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), index=True, comment="소프트 삭제 시각(NULL=정상)",
    )

    call: Mapped["Call"] = relationship(back_populates="sentences")
    # 1:1 평가(자식). 발화 삭제 시 평가도 함께(delete-orphan + DB CASCADE)
    evaluation: Mapped[Optional["Evaluation"]] = relationship(
        back_populates="sentence",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    reviews: Mapped[list["Review"]] = relationship(
        back_populates="sentence", cascade="all, delete-orphan", passive_deletes=True,
    )
