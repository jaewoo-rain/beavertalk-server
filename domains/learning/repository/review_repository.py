"""ReviewRepository — 복습 추가/조회."""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from domains.learning.models.review import Review
from domains.learning.models.sentence import Sentence


class ReviewRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, review: Review) -> Review:
        self.db.add(review)
        return review

    def get(self, review_id: int) -> Optional[Review]:
        # 소유 검증(발화→통화) + 문장 정보(korean/native) 함께 로드
        return self.db.get(
            Review,
            review_id,
            options=[joinedload(Review.sentence).joinedload(Sentence.call)],
        )

    def list_by_sentence(self, sentence_id: int) -> Sequence[Review]:
        stmt = (
            select(Review)
            .where(Review.sentence_id == sentence_id)
            .order_by(Review.record_time.desc().nullslast(), Review.review_id.desc())
        )
        return self.db.scalars(stmt).all()
