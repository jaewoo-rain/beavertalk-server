"""IAP(인앱결제) DTO.

계약 문서: docs/20260731_1230_IAP-API-계약서-프론트공유용.md
이 파일의 필드명·형태는 **앱과의 계약**이다. 바꾸려면 문서·앱을 함께 고쳐야 한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class PurchaseItem(BaseModel):
    """스토어 결제 1건의 영수증."""

    product_id: str = Field(min_length=1)  # 예: im.beavertalk.character.bibi
    # iOS: originalTransactionId / Android: orderId. 멱등 키의 재료다.
    transaction_id: str = Field(min_length=1)
    # iOS: StoreKit2 Transaction.jwsRepresentation / Android: Purchase.purchaseToken
    purchase_token: str = Field(min_length=1)


class VerifyRequest(PurchaseItem):
    """POST /purchases/verify — 결제 직후 1건 검증."""

    platform: Literal["ios", "android"]
    is_sandbox: bool = False


class RestoreRequest(BaseModel):
    """POST /purchases/restore — 폰 교체·재설치 후 과거 영수증 일괄 복원."""

    platform: Literal["ios", "android"]
    purchases: list[PurchaseItem] = Field(min_length=1)
    is_sandbox: bool = False


class Entitlement(BaseModel):
    """이 회원이 **지금** 가진 권한. "Pro 인가"의 단일 진실.

    ⚠ 앱이 pro_expires_at 을 자체 비교해 Pro 여부를 정하면 안 된다 — 기기 시계 조작·
    시차로 어긋난다. is_pro 를 그대로 쓰고 만료 시각은 표시용으로만.
    """

    is_pro: bool
    pro_expires_at: Optional[datetime] = None
    owned_character_ids: list[int] = []


class VerifyResponse(BaseModel):
    """검증 성공 응답. 실패는 4xx/5xx 로 나간다.

    already_granted=True 도 **정상 성공(200)** 이다 — 재시도·앱 재실행·복원으로 같은
    영수증이 여러 번 오는 건 정상 동작이라 에러로 다루면 안 된다(멱등).
    """

    status: Literal["granted"] = "granted"
    already_granted: bool = False
    product_id: str
    kind: Literal["character", "subscription"]
    character_id: Optional[int] = None  # 구독이면 None
    entitlement: Entitlement


class RestoreResponse(BaseModel):
    """일부가 무효여도 200 — failed 로 알려주고 유효한 것만 지급한다."""

    restored: int
    failed: int
    entitlement: Entitlement
