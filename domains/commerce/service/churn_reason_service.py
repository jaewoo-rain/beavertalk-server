"""ChurnReasonService — 해지 사유 저장(§17). 트랜잭션 경계(R3 — service 가 commit)."""

from __future__ import annotations

import logging

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from domains.commerce.repository.churn_reason_repository import ChurnReasonRepository
from domains.commerce.repository.subscribe_repository import SubscribeRepository
from domains.commerce.schemas.churn_reason import ChurnReasonIn, ChurnReasonOut

logger = logging.getLogger(__name__)


class ChurnReasonService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = ChurnReasonRepository(db)
        self.subscribe_repo = SubscribeRepository(db)

    def submit(self, member_id: int, data: ChurnReasonIn) -> ChurnReasonOut:
        """유니크 키는 (member_id, subscribe_id) — 「어느 구독이 끝났나」로 식별한다.

        ⛔ 서버가 resolve_status 로 "지금 만료된 구독"을 스스로 추론하지 않는다 —
        회원이 답하기 전에 재구독하면 그 판정이 expired 가 아니게 되어 직전 만료에
        대한 정당한 응답을 거절하거나 엉뚱한 행에 붙이게 된다. 앱이 이미 들고 있는
        subscribe_id(subscription_status_dto.dart)를 그대로 받고, 서버는 **소유만**
        검증한다(위조 불가) — SubscriptionService.cancel 과 같은 패턴.
        """
        sub = self.subscribe_repo.get(data.subscribe_id)
        if sub is None or sub.member_id != member_id:
            # 존재를 알려주지 않는다(이 저장소의 소유 검증 관례 — call_service._assert_owner
            # ·subscription_service.cancel 과 같은 404).
            raise HTTPException(status.HTTP_404_NOT_FOUND, "구독을 찾을 수 없습니다.")

        row = self.repo.upsert(
            member_id=member_id,
            subscribe_id=data.subscribe_id,
            reason=data.reason,
            offer_shown=data.offer_shown,
            end_date_snapshot=sub.end_date,
        )
        self.db.commit()
        self.db.refresh(row)
        logger.info(
            "churn_reason: member=%s subscribe=%s reason=%s offer_shown=%s",
            member_id, data.subscribe_id, data.reason, data.offer_shown,
        )
        return ChurnReasonOut.model_validate(row)
