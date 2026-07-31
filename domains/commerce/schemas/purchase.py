"""구매/결제 DTO."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict


class PurchaseRequest(BaseModel):
    """구매 요청(선택). 카드 정보는 PG 토큰화 후 마스킹값을 프론트가 전달."""

    card_info: Optional[str] = None
    # 사용자가 화면에서 **본** 가격. 가격은 서버가 정하지만(effective_price), 한정 할인이
    # 탭하는 사이 끝나면 화면엔 $5 인데 $10 이 청구된다. 이 값이 오면 서버 계산과 대조해
    # 다르면 409 로 거절한다 — 사용자가 동의하지 않은 금액을 결제하지 않기 위해서다.
    # 미전송(구버전 앱)이면 검사를 건너뛴다(하위호환).
    expected_price: Optional[Decimal] = None


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payment_id: int
    payment_date: Optional[datetime]
    price: Optional[Decimal]
    description: Optional[str]


class MemberCharacterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    member_id: int
    character_id: int
    purchase_price: Optional[Decimal]
    purchase_date: Optional[datetime]


class PurchaseResponse(BaseModel):
    """구매 결과 — 소유 레코드 + 결제 레코드 동시 반환."""

    member_character: MemberCharacterOut
    payment: PaymentOut
    # ⚠ IAP 전환 전 임시 필드. True = **실결제가 아니라 테스트 지급**이다
    # (서버가 카드사·PG·스토어 어디에도 청구하지 않았다). 앱은 이 값으로
    # "테스트 구매" 배지를 띄우거나, 개발 빌드에서만 허용하는 식으로 쓸 수 있다.
    # IAP(POST /purchases/verify)로 넘어가면 이 필드는 사라진다.
    is_test_grant: bool = False
