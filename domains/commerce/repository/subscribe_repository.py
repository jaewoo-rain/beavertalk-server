"""SubscribeRepository — 구독 추가/조회."""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from domains.commerce.models.subscribe import Subscribe


class SubscribeRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, subscribe_id: int) -> Optional[Subscribe]:
        return self.db.get(Subscribe, subscribe_id)

    def list_by_member(self, member_id: int) -> Sequence[Subscribe]:
        stmt = (
            select(Subscribe)
            .where(Subscribe.member_id == member_id)
            .order_by(Subscribe.subscribe_id.desc())
        )
        return self.db.scalars(stmt).all()

    def add(self, subscribe: Subscribe) -> Subscribe:
        self.db.add(subscribe)
        return subscribe
