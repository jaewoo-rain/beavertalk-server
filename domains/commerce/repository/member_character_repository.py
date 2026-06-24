"""MemberCharacterRepository — 소유(구매) 레코드 조회/추가."""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from domains.commerce.models.member_character import MemberCharacter


class MemberCharacterRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, member_id: int, character_id: int) -> Optional[MemberCharacter]:
        # 복합 PK는 (선두컬럼, 다음컬럼) 순서의 튜플로 조회
        return self.db.get(MemberCharacter, (member_id, character_id))

    def owned_character_ids(self, member_id: int) -> set[int]:
        stmt = select(MemberCharacter.character_id).where(
            MemberCharacter.member_id == member_id
        )
        return set(self.db.scalars(stmt).all())

    def list_by_member(self, member_id: int) -> Sequence[MemberCharacter]:
        stmt = (
            select(MemberCharacter)
            .where(MemberCharacter.member_id == member_id)
            .options(selectinload(MemberCharacter.character))  # 캐릭터 정보 N+1 방지
            .order_by(MemberCharacter.character_id)
        )
        return self.db.scalars(stmt).all()

    def add(self, mc: MemberCharacter) -> MemberCharacter:
        self.db.add(mc)
        return mc
