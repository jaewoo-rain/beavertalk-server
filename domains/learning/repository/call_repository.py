"""CallRepository — 통화 조회/추가/삭제."""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from domains.learning.models.call import Call
from domains.learning.models.sentence import Sentence


class CallRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_basic(self, call_id: int) -> Optional[Call]:
        """소유 검증·rating 수정용(연관 미로딩)."""
        return self.db.get(Call, call_id)

    def get_detail(self, call_id: int) -> Optional[Call]:
        """상세용 — 발화(컬렉션)=selectin, 그 안 평가(스칼라)=joined, 캐릭터=joined."""
        return self.db.get(
            Call,
            call_id,
            options=[
                joinedload(Call.character),
                selectinload(Call.sentences).joinedload(Sentence.evaluation),
            ],
        )

    def get_with_raw(self, call_id: int) -> Optional[Call]:
        return self.db.get(Call, call_id, options=[selectinload(Call.raw_data)])

    def list_by_member(
        self, member_id: int, limit: int = 20, offset: int = 0
    ) -> Sequence[Call]:
        stmt = (
            select(Call)
            .where(Call.member_id == member_id)
            .options(joinedload(Call.character))  # 목록엔 캐릭터만(발화 미포함)
            .order_by(Call.call_date.desc(), Call.call_id.desc())
            .limit(limit)
            .offset(offset)
        )
        return self.db.scalars(stmt).all()

    def has_call_in_window(
        self, member_id: int, start_utc, end_utc, min_seconds: int
    ) -> bool:
        """[start_utc, end_utc) 안에 min_seconds 초 이상 유효 통화가 있는지(EXISTS).

        유효 = total_time>=min_seconds AND status in(done, analyzing)(ongoing/failed 제외).
        '오늘 통화함' 파생용 — member 컬럼/일일 초기화 없이 call 에서 계산.
        """
        inner = select(Call.call_id).where(
            Call.member_id == member_id,
            Call.call_date >= start_utc,
            Call.call_date < end_utc,
            Call.total_time >= min_seconds,
            Call.status.in_(("done", "analyzing")),
        )
        return bool(self.db.scalar(select(inner.exists())))

    def add(self, call: Call) -> Call:
        self.db.add(call)
        return call

    def delete(self, call: Call) -> None:
        self.db.delete(call)
