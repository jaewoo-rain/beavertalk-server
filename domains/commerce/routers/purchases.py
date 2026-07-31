"""IAP 구매 라우터 — 영수증 검증 / 권한 조회 / 복원.

라우터는 얇게: DTO 검증 + 인증 + 서비스 호출만 한다(CLAUDE.md 레이어 규율).
계약: docs/20260731_1230_IAP-API-계약서-프론트공유용.md
"""

from __future__ import annotations

from fastapi import APIRouter, status

from core.deps import CurrentMember, DbSession
from domains.commerce.schemas.iap import (
    Entitlement,
    RestoreRequest,
    RestoreResponse,
    VerifyRequest,
    VerifyResponse,
)
from domains.commerce.service.iap_service import IapService

router = APIRouter(prefix="/purchases", tags=["purchases"])


@router.post("/verify", response_model=VerifyResponse, status_code=status.HTTP_200_OK)
def verify_purchase(
    data: VerifyRequest, member: CurrentMember, db: DbSession
) -> VerifyResponse:
    """스토어 결제 직후 영수증을 검증하고 지급한다.

    ⭐ **already_granted=True 도 200 성공**이다 — 재시도·앱 재실행·복원으로 같은
      영수증이 여러 번 오는 건 정상 동작이라 에러로 다루면 안 된다.

    실패 코드(앱은 message 가 아니라 code 로 분기할 것):
      404 UNKNOWN_PRODUCT        서버가 모르는 상품 ID
      422 INVALID_RECEIPT        스토어가 무효 판정 — **재시도 무의미**
      409 RECEIPT_OWNED_BY_OTHER 다른 계정이 쓴 영수증
      503 VERIFY_UNAVAILABLE     스토어 응답 없음 — **재시도 가능**(백오프)
    """
    return IapService(db).verify_and_grant(
        member.member_id, data.platform, data, data.is_sandbox
    )


@router.get("/entitlement", response_model=Entitlement)
def get_entitlement(member: CurrentMember, db: DbSession) -> Entitlement:
    """현재 권한 — "이 회원이 지금 Pro 인가"의 단일 진실.

    앱 시작 시·복원 후·구독 화면 진입 시 호출한다. 구독은 갱신·해지·환불이 앱 밖에서
    일어나므로(스토어) 앱이 자체 판단하면 실제와 어긋난다.
    """
    return IapService(db).entitlement(member.member_id)


@router.post("/restore", response_model=RestoreResponse)
def restore_purchases(
    data: RestoreRequest, member: CurrentMember, db: DbSession
) -> RestoreResponse:
    """폰 교체·재설치 후 과거 구매 복원(캐릭터는 영구 소유).

    일부가 무효여도 200 — failed 로 알려주고 유효한 것만 지급한다.
    """
    return IapService(db).restore(
        member.member_id, data.platform, data.purchases, data.is_sandbox
    )
