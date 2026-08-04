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
from domains.commerce.schemas.subscription import (
    SubscribeCreate,
    SubscriptionOut,
    SubscriptionStatusOut,
)
from domains.commerce.service.subscription_status import resolve_status


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
            "subscription: 결제 미연동 구독 시작 member=%s price=%s plan=%s ⚠ 실결제 아님(IAP 전환 대기)",
            member_id, data.price, data.plan,
        )

        # ⛔ 중복 활성 행 차단. 그동안 검사가 없어서 회원당 활성 구독이 여러 개 생길 수
        #   있었고, 그게 상태 판정 모호성의 원천이었다(앱 resolver 도 "여러 개일 수
        #   있다"를 전제로 짜여 있다). 상태 8종을 내리기 시작하는 이상, 어느 행이
        #   진짜인지가 갈리면 안 된다.
        #   ⚠ DB 부분 UNIQUE 인덱스는 기존 중복 데이터 때문에 아직 못 건다(파괴적
        #     정리가 선행돼야 함 — R6). 그때까지 이 검사가 유일한 방어선이다.
        if self.repo.find_active(member_id) is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "SUBSCRIPTION_ALREADY_ACTIVE",
                    "message": "이미 활성 구독이 있습니다.",
                },
            )

        now = datetime.now(timezone.utc)
        sub = Subscribe(
            member_id=member_id,
            start_date=data.start_date or now,
            end_date=data.end_date,
            price=data.price,
            is_activate=True,
            plan=data.plan,
            billing_period=data.billing_period,
            source="manual",  # 결제 없이 만든 행 — 정산에서 걸러야 한다
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

    def status(self, member_id: int) -> SubscriptionStatusOut:
        """회원 단위 현재 상태 1건 — 상태의 권위는 서버다.

        앱이 price 같은 값으로 상태를 역추론하면 해지 안내가 틀어진다. 판정 규칙은
        subscription_status.resolve_status 한 곳에만 둔다.
        """
        resolved = resolve_status(self.repo.list_by_member(member_id))
        return SubscriptionStatusOut(
            state=resolved.state,  # type: ignore[arg-type]
            plan=resolved.plan,  # type: ignore[arg-type]
            subscribe_id=resolved.subscribe_id,
            price=resolved.price,
            start_date=resolved.start_date,
            end_date=resolved.end_date,
            retrying_until=resolved.retrying_until,
            paused_since=resolved.paused_since,
        )

    def cancel(self, member_id: int, subscribe_id: int) -> SubscriptionOut:
        sub = self.repo.get(subscribe_id)
        if sub is None or sub.member_id != member_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "구독을 찾을 수 없습니다.")
        sub.is_activate = False  # 삭제 아님 — 이력 보존
        self.db.commit()
        self.db.refresh(sub)
        return SubscriptionOut.model_validate(sub)
