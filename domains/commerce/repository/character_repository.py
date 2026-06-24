"""CharacterRepository — 캐릭터 조회."""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from domains.commerce.models.character import Character


class CharacterRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, character_id: int) -> Optional[Character]:
        # 상세/구매에서 할인(discount_events)을 바로 쓰므로 함께 eager 로드
        # (단건이라 N+1은 아니지만, 별도 지연쿼리·세션종료 후 접근을 예방)
        return self.db.get(
            Character,
            character_id,
            options=[selectinload(Character.discount_events)],
        )

    def list(self, limit: int = 20, offset: int = 0) -> Sequence[Character]:
        stmt = (
            select(Character)
            .order_by(Character.character_id)
            .limit(limit)
            .offset(offset)
            # 목록에서 캐릭터별 할인 조회 N+1 방지
            .options(selectinload(Character.discount_events))
        )
        return self.db.scalars(stmt).all()
