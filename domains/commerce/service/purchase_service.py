"""PurchaseService — 캐릭터 구매(중복 방지 + member_character·payment 동시 생성).

핵심: 소유 레코드와 결제 레코드를 **한 트랜잭션**으로 묶어 둘 다 성공 or 둘 다 롤백.
가격은 서버가 결정(클라이언트 변조 방지).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from domains.commerce.models.member_character import MemberCharacter
from domains.commerce.models.payment import Payment
from domains.commerce.repository.character_repository import CharacterRepository
from domains.commerce.repository.member_character_repository import (
    MemberCharacterRepository,
)
from domains.commerce.repository.payment_repository import PaymentRepository
from domains.commerce.schemas.purchase import (
    MemberCharacterOut,
    PaymentOut,
    PurchaseResponse,
)
from domains.commerce.service.character_service import CharacterService


class PurchaseService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.char_repo = CharacterRepository(db)
        self.mc_repo = MemberCharacterRepository(db)
        self.payment_repo = PaymentRepository(db)
        self.char_service = CharacterService(db)

    def purchase(
        self,
        member_id: int,
        character_id: int,
        card_info: str | None = None,
        expected_price: Decimal | None = None,
    ) -> PurchaseResponse:
        character = self.char_repo.get(character_id)
        if character is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "캐릭터를 찾을 수 없습니다.")

        # 중복 구매 방지(복합 PK 충돌 전에 친절히)
        if self.mc_repo.get(member_id, character_id) is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"code": "ALREADY_OWNED", "message": "이미 보유한 캐릭터입니다."},
            )

        price = self.char_service.effective_price(character)  # 서버가 가격 결정

        # 가격 경합 방어: 한정 할인이 "구매" 탭과 서버 처리 사이에 끝나면, 사용자는 할인가를
        # 보고 눌렀는데 정가가 청구된다. 클라가 본 가격을 보내오면 대조해 다르면 거절하고
        # 실제 가격을 알려준다 — 앱이 새 가격으로 다시 확인받게 하려는 것.
        if expected_price is not None and Decimal(expected_price) != price:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "PRICE_CHANGED",
                    "message": "가격이 변경되었습니다. 다시 확인해 주세요.",
                    "expected_price": str(expected_price),
                    "actual_price": str(price),
                },
            )

        now = datetime.now(timezone.utc)

        mc = MemberCharacter(
            member_id=member_id,
            character_id=character_id,
            purchase_price=price,
            purchase_date=now,
        )
        payment = Payment(
            member_id=member_id,
            price=price,
            payment_date=now,
            description=f"캐릭터 구매: {character.name}",
            category="character",
            card_info=card_info,
        )
        self.mc_repo.add(mc)
        self.payment_repo.add(payment)
        self.db.commit()  # ← 둘을 한 트랜잭션으로. 중간 실패 시 전부 롤백
        self.db.refresh(mc)
        self.db.refresh(payment)

        return PurchaseResponse(
            member_character=MemberCharacterOut.model_validate(mc),
            payment=PaymentOut.model_validate(payment),
        )
