"""SubscriptionService — 구독 시작/취소. 시작 시 payment(category=subscribe) 동시 생성."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from domains.commerce.models.payment import Payment
from domains.commerce.models.subscribe import Subscribe
from domains.commerce.repository.payment_repository import PaymentRepository
from domains.commerce.repository.subscribe_repository import SubscribeRepository
from domains.commerce.schemas.subscription import SubscribeCreate, SubscriptionOut


class SubscriptionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = SubscribeRepository(db)
        self.payment_repo = PaymentRepository(db)

    def start(self, member_id: int, data: SubscribeCreate) -> SubscriptionOut:
        now = datetime.now(timezone.utc)
        sub = Subscribe(
            member_id=member_id,
            start_date=data.start_date or now,
            end_date=data.end_date,
            price=data.price,
            is_activate=True,
        )
        payment = Payment(
            member_id=member_id,
            price=data.price,
            payment_date=now,
            description="구독 결제",
            category="subscribe",
            card_info=data.card_info,
        )
        self.repo.add(sub)
        self.payment_repo.add(payment)
        self.db.commit()  # 구독 + 결제 한 트랜잭션
        self.db.refresh(sub)
        return SubscriptionOut.model_validate(sub)

    def list(self, member_id: int) -> list[SubscriptionOut]:
        return [SubscriptionOut.model_validate(s) for s in self.repo.list_by_member(member_id)]

    def cancel(self, member_id: int, subscribe_id: int) -> SubscriptionOut:
        sub = self.repo.get(subscribe_id)
        if sub is None or sub.member_id != member_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "구독을 찾을 수 없습니다.")
        sub.is_activate = False  # 삭제 아님 — 이력 보존
        self.db.commit()
        self.db.refresh(sub)
        return SubscriptionOut.model_validate(sub)
