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

    def find_active(self, member_id: int) -> Optional[Subscribe]:
        """활성 플래그가 선 가장 최근 행(만료 여부는 보지 않는다).

        중복 활성 행 차단용이다 — 만료된 활성 행도 갱신 대상이지 새로 만들 이유가
        없으므로, 여기서는 날짜를 따지지 않고 플래그만 본다.
        """
        stmt = (
            select(Subscribe)
            .where(Subscribe.member_id == member_id, Subscribe.is_activate.is_(True))
            .order_by(Subscribe.subscribe_id.desc())
            .limit(1)
        )
        return self.db.scalars(stmt).first()

    def add(self, subscribe: Subscribe) -> Subscribe:
        self.db.add(subscribe)
        return subscribe
