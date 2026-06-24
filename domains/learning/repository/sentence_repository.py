"""SentenceRepository — 발화 조회(소유 검증은 call 경유)."""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from domains.learning.models.call import Call
from domains.learning.models.sentence import Sentence


class SentenceRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, sentence_id: int) -> Optional[Sentence]:
        # 소유 검증용 call + 응답용 evaluation 함께 로드
        return self.db.get(
            Sentence,
            sentence_id,
            options=[joinedload(Sentence.call), joinedload(Sentence.evaluation)],
        )

    def list_bookmarked(self, member_id: int) -> Sequence[Sentence]:
        # sentence → call 조인으로 내 북마크만. 평가는 joined.
        stmt = (
            select(Sentence)
            .join(Sentence.call)
            .where(
                Call.member_id == member_id,
                Sentence.is_bookmarked.is_(True),
                Sentence.deleted_at.is_(None),  # 소프트 삭제 제외
            )
            .options(joinedload(Sentence.evaluation))
            .order_by(Sentence.sentence_id.desc())
        )
        return self.db.scalars(stmt).all()
