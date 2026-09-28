"""ChurnReasonRepository — 해지 사유 upsert(순수 DB 접근, commit 안 함 — R3)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from domains.commerce.models.churn_reason import ChurnReason


class ChurnReasonRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def upsert(
        self,
        member_id: int,
        subscribe_id: int,
        reason: str,
        offer_shown: bool,
        end_date_snapshot: Optional[datetime],
    ) -> ChurnReason:
        """(member_id, subscribe_id) 에 기존 행이 있으면 갱신, 없으면 생성.

        재설치 후 다른 답을 고르면 그게 최신 의사다 — "마지막 값" 갱신(ON CONFLICT
        DO UPDATE 와 같은 뜻, 다만 앱이 짧은 기간에 같은 (member, subscribe) 조합을
        여러 번 보낼 일이 드물어 SELECT-then-write 로 충분하다 — 경합이 없는
        회원 자기 자신의 응답 갱신).
        """
        row = self.db.scalar(
            select(ChurnReason).where(
                ChurnReason.member_id == member_id,
                ChurnReason.subscribe_id == subscribe_id,
            )
        )
        if row is None:
            row = ChurnReason(member_id=member_id, subscribe_id=subscribe_id)
            self.db.add(row)
        row.reason = reason
        row.offer_shown = offer_shown
        row.end_date_snapshot = end_date_snapshot
        return row
