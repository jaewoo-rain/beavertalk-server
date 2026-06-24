"""PurchaseService — 캐릭터 구매(중복 방지 + member_character·payment 동시 생성).

핵심: 소유 레코드와 결제 레코드를 **한 트랜잭션**으로 묶어 둘 다 성공 or 둘 다 롤백.
가격은 서버가 결정(클라이언트 변조 방지).
"""

from __future__ import annotations

from datetime import datetime, timezone

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
        self, member_id: int, character_id: int, card_info: str | None = None
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
