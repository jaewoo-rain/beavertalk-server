"""SubscriptionService — 구독 시작/취소. 시작 시 payment(category=subscribe) 동시 생성."""

from __future__ import annotations

import logging

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from domains.commerce.models.payment import Payment
from domains.commerce.models.subscribe import Subscribe
from domains.commerce.repository.payment_repository import PaymentRepository
from domains.commerce.repository.subscribe_repository import SubscribeRepository
from domains.commerce.schemas.subscription import SubscribeCreate, SubscriptionOut


logger = logging.getLogger(__name__)


class SubscriptionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = SubscribeRepository(db)
        self.payment_repo = PaymentRepository(db)

    def start(self, member_id: int, data: SubscribeCreate) -> SubscriptionOut:
        # ── 결제 미연동 상태 (IAP 전환 전) ──────────────────────────────── #
        # 🧒 캐릭터 구매와 같다 — 여기도 실제 청구가 없다. 게다가 **구독료를 클라가
        #   정한다**(SubscribeCreate.price, 검증은 gt=0 뿐). 즉 0.01 을 보내면 1센트에 Pro 다.
        #
        # 🧒 그래도 막지 않는 이유: 앱이 구독 흐름을 지금 만들어야 하기 때문. 대신
        #   경고 로그를 남겨 실결제와 구분한다.
        #
        # ⚠ 이 API 는 IAP 전환 시 **폐기**된다 — 가격·기간·갱신을 전부 스토어가 정하므로
        #   클라가 금액을 보내는 구조와 양립하지 않는다. 대체: POST /purchases/verify.
        #   근거: docs/20260731_1230_IAP-API-계약서-프론트공유용.md
        logger.warning(
            "subscription: 결제 미연동 구독 시작 member=%s price=%s ⚠ 실결제 아님(IAP 전환 대기)",
            member_id, data.price,
        )

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
