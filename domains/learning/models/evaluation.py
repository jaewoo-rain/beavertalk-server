"""evaluation (평가) — learning 도메인. sentence 와 1:1 (evaluation 이 자식)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, ForeignKey, Identity, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.learning.models.sentence import Sentence


class Evaluation(Base, TimestampMixin):
    __tablename__ = "evaluation"

    evaluation_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # 발화의 자식(1:1) — UNIQUE FK 로 "발화당 평가 1건" 보장, 발화 삭제 시 CASCADE
    sentence_id: Mapped[int] = mapped_column(
        ForeignKey("sentence.sentence_id", ondelete="CASCADE"), unique=True, comment="발화",
    )
    total_score: Mapped[Optional[int]] = mapped_column(Integer, comment="총 점수")
    pronunciation: Mapped[Optional[int]] = mapped_column(Integer, comment="발음")
    fluency: Mapped[Optional[int]] = mapped_column(Integer, comment="유창성")
    rhythm: Mapped[Optional[int]] = mapped_column(Integer, comment="리듬")

    sentence: Mapped["Sentence"] = relationship(back_populates="evaluation")
