"""MemberRepository — 순수 DB 접근 계층.

Spring Data JPA 의 Repository 에 해당하지만, SQLAlchemy 는 자동 생성이 없으므로
쿼리를 직접 작성한다. **여기서는 commit 하지 않는다** (트랜잭션 경계는 service 가 관리).
세션은 외부(service)에서 주입받는다.
"""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from domains.account.models.member import Member


class MemberRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, member_id: int) -> Optional[Member]:
        """PK 로 단건 조회 (Session.get = 1차 캐시 활용)."""
        return self.db.get(Member, member_id)

    def get_with_speak_country(self, member_id: int) -> Optional[Member]:
        """마이페이지용 — 억양(M:1)을 함께 로드."""
        return self.db.get(
            Member, member_id, options=[joinedload(Member.speak_country)]
        )

    def get_by_email(self, email: str) -> Optional[Member]:
        stmt = select(Member).where(Member.email == email)
        return self.db.scalar(stmt)

    def get_by_auth(self, auth_user_id: str) -> Optional[Member]:
        """Supabase auth.users.id(UUID) 로 member 조회."""
        stmt = select(Member).where(Member.auth_user_id == auth_user_id)
        return self.db.scalar(stmt)

    def list(self, limit: int = 50, offset: int = 0) -> Sequence[Member]:
        stmt = select(Member).order_by(Member.member_id).limit(limit).offset(offset)
        return self.db.scalars(stmt).all()

    def add(self, member: Member) -> Member:
        """세션에 추가만 (flush/commit 은 service 책임)."""
        self.db.add(member)
        return member

    def delete(self, member: Member) -> None:
        self.db.delete(member)
