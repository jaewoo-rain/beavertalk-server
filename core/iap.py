"""IAP 영수증 검증 어댑터 — 애플/구글 + 스텁.

🧒 이 파일이 하는 일 한 줄:
    "앱이 준 영수증이 진짜인가?"를 판정한다. 앱 말을 믿지 않고 스토어에 직접 묻는 게
    IAP 백엔드의 존재 이유다(앱은 사용자 기기에서 돌아 위조가 가능하다).

지금은 **스텁 모드**다. 애플 .p8 키·구글 서비스계정이 아직 없어서 실제 호출을 못 한다.
그래서 형식만 검사하고 통과시킨다 — 프론트가 결제 흐름 전 구간(구매→지급→화면 갱신→
복원)을 돌려볼 수 있게 하려는 것. **계약(요청·응답 형태)은 실제와 동일**하므로, 키가
들어오면 이 파일의 _verify_apple / _verify_google 안쪽만 채우면 앱은 손댈 게 없다.

⛔ 스텁은 서명을 안 본다 = 아무 문자열이나 통과한다. 절대 그대로 운영에 쓰면 안 된다.
   settings.IAP_VERIFY_ENABLED=True + 키 배포로 전환한다.

graceful 규율(R5): 키 부재·네트워크 오류는 예외를 던지지 않고 결과 객체로 돌려준다 —
호출부가 422(무효)와 503(일시 실패)을 구분해 응답할 수 있어야 하기 때문.

계약: docs/20260731_1230_IAP-API-계약서-프론트공유용.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Optional

from core.config import settings

logger = logging.getLogger(__name__)

Platform = Literal["ios", "android"]


@dataclass(frozen=True)
class VerifyResult:
    """검증 결과. ok=False 면 reason 이 호출부의 HTTP 상태를 결정한다.

    reason 값:
      "invalid"     → 422 INVALID_RECEIPT   (스토어가 무효 판정 — 재시도 무의미)
      "unavailable" → 503 VERIFY_UNAVAILABLE(스토어 응답 없음 — 재시도 가능)
    """

    ok: bool
    reason: Optional[str] = None
    # 스토어가 알려준 정규 거래 id(멱등 키). 스텁은 요청값을 그대로 돌려준다.
    transaction_id: Optional[str] = None
    # 구독일 때 만료 시각(UTC). 캐릭터는 None.
    expires_at: Optional[object] = None
    # 스텁으로 통과했는지(로그·응답 진단용).
    stubbed: bool = False


def verify(
    platform: Platform,
    product_id: str,
    transaction_id: str,
    purchase_token: str,
    is_sandbox: bool = False,
) -> VerifyResult:
    """영수증 1건을 검증한다.

    실검증이 켜져 있으면(IAP_VERIFY_ENABLED) 플랫폼별 어댑터로, 아니면 스텁으로 간다.
    """
    if settings.IAP_VERIFY_ENABLED:
        if platform == "ios":
            return _verify_apple(product_id, transaction_id, purchase_token, is_sandbox)
        return _verify_google(product_id, transaction_id, purchase_token, is_sandbox)

    if not settings.IAP_ALLOW_STUB:
        # 실검증도 꺼져 있고 스텁도 금지 = 결제를 받을 수 없는 상태.
        logger.error("iap: 검증 비활성 + 스텁 금지 → 결제 불가(설정 확인)")
        return VerifyResult(ok=False, reason="unavailable")

    return _verify_stub(platform, product_id, transaction_id, purchase_token)


def _verify_stub(
    platform: Platform, product_id: str, transaction_id: str, purchase_token: str
) -> VerifyResult:
    """개발·QA용 가짜 검증.

    🧒 왜 이런 게 필요한가: 스토어 자격증명이 없으면 실제 검증을 못 하는데, 그렇다고
      결제 API 를 막아두면 **앱이 결제 흐름을 하나도 못 만든다**. 계약대로 응답하는
      가짜를 두면 앱은 구매→지급→복원까지 전부 구현·테스트할 수 있고, 나중에 서버만
      진짜로 바꾸면 된다.

    형식 검사는 한다 — 앱의 필드 누락·오타를 여기서 잡아야 나중에 진짜로 바꿨을 때
    "갑자기 422 가 쏟아지는" 일이 없다.

    ⭐ 테스트 편의: purchase_token 이 "invalid" / "unavailable" 로 시작하면 그 실패를
      흉내낸다. 앱이 422·503 분기(재시도 여부)를 실제로 짜볼 수 있어야 하기 때문이다.
    """
    tok = purchase_token.strip()
    if tok.startswith("invalid"):
        return VerifyResult(ok=False, reason="invalid", stubbed=True)
    if tok.startswith("unavailable"):
        return VerifyResult(ok=False, reason="unavailable", stubbed=True)
    if not tok or not transaction_id.strip() or not product_id.strip():
        return VerifyResult(ok=False, reason="invalid", stubbed=True)

    logger.info(
        "iap(stub): 통과 platform=%s product=%s tx=%s ⚠ 실검증 아님",
        platform, product_id, transaction_id,
    )
    return VerifyResult(ok=True, transaction_id=transaction_id, stubbed=True)


def _verify_apple(
    product_id: str, transaction_id: str, purchase_token: str, is_sandbox: bool
) -> VerifyResult:
    """App Store 영수증 검증 (미구현 — 자격증명 대기).

    구현 시: purchase_token 은 StoreKit2 의 JWS(JSON Web Signature)다. 애플 루트
    인증서로 서명 체인을 검증하고 페이로드에서 productId·originalTransactionId·
    expiresDate 를 꺼낸다. App Store Server API 키(.p8 + Issuer ID + Key ID)가 필요하고,
    샌드박스와 운영은 검증 엔드포인트가 다르다.
    """
    logger.warning("iap(apple): 미구현 — 자격증명 대기")
    return VerifyResult(ok=False, reason="unavailable")


def _verify_google(
    product_id: str, transaction_id: str, purchase_token: str, is_sandbox: bool
) -> VerifyResult:
    """Google Play 영수증 검증 (미구현 — 자격증명 대기).

    구현 시: Play Developer API 의 purchases.products.get / purchases.subscriptionsv2.get
    으로 purchaseToken 을 조회한다. Play Console 권한이 있는 서비스계정이 필요하다.
    subscriptionsv2.get 응답의 subscriptionState=="SUBSCRIPTION_STATE_ACTIVE" 이고
    lineItems[].expiryTime 이 지금보다 미래면 유효로 본다.

    ⛔ 검증 후 **반드시 acknowledge** 를 보내야 한다(아래 acknowledge() — 지급이
       끝난 뒤에만 부른다). 3일 안에 안 하면 구글이 자동 환불하고, 돈은 돌아가는데
       지급은 남는 사고가 난다(애플엔 없는 절차 — acknowledge() 의 애플 분기 주석 참조).
    """
    logger.warning("iap(google): 미구현 — 자격증명 대기")
    return VerifyResult(ok=False, reason="unavailable")


def acknowledge(
    platform: Platform, kind: str, purchase_token: str, product_id: str
) -> bool:
    """지급 성공 **다음에만** 부른다 — 순서가 곧 환불 방지 설계다.

    ⛔⛔ **지급 전에 부르면 안 된다.** acknowledge 가 먼저 나가고 지급이 그 뒤에
    실패하면(DB 오류 등) 구글은 "확인된 결제"로 알고 환불 창구가 막힌다 — 돈은
    받았는데 지급도 못 하고 환불도 못 받는 사고. `iap_service.verify_and_grant` 는
    `db.commit()` 성공 뒤에만 이 함수를 부른다.

    실패해도 이미 커밋된 지급을 되돌리지 않는다(R5) — 구글이 3일의 여유를 주므로
    다음 acknowledge 재시도(배치·재요청)로 복구할 수 있다. 실패를 로그로 남겨
    추적 가능하게만 한다.

    - **구글**: kind=="subscription" 이면 v1 `purchases.subscriptions.acknowledge`,
      kind=="character"(일회성 상품) 면 `purchases.products.acknowledge` — 엔드포인트가
      다르다(둘 다 v1, subscriptionsv2 조회와는 별개 API 계열).
    - **애플**: acknowledge 류 절차가 **없는 것으로 보인다** — 구현 전 재확인 시도:
      Apple 공식 문서(App Store Server API·Server Notifications)는 JS 렌더링이라
      이 세션의 WebFetch 로 원문을 못 읽었다(제목만 반환됨). WebSearch 로 찾은 간접
      근거: 애플의 환불 관련 절차는 「CONSUMPTION_REQUEST 알림(실제 환불 *요청*이
      있을 때만 발생) → 서버가 12시간 안에 소비 데이터를 제출해 환불 결정에 참고
      자료로 반영」이다 — 이건 "일반 구매를 N일 안에 확인 안 하면 자동환불"이 아니라
      "이미 들어온 환불 요청에 대한 선택적 소명" 이라 구글의 acknowledge 와 성격이
      다르다. ⇒ **명시적 부정문을 원문에서 확인하지 못했다 — 추측을 사실로 적지
      않는다.** 애플 키가 들어오는 시점에 원문을 다시 확인해라.
    """
    if platform != "android":
        logger.info("iap(apple): acknowledge 미대상(애플엔 이 절차가 없는 것으로 보임, 위 docstring 참조)")
        return True
    if not settings.IAP_VERIFY_ENABLED:
        logger.info("iap(google): 검증 비활성 상태 → acknowledge 스킵(스텁 지급)")
        return True
    return _acknowledge_google(kind, purchase_token, product_id)


def _acknowledge_google(kind: str, purchase_token: str, product_id: str) -> bool:
    """Google Play acknowledge (미구현 — 자격증명 대기)."""
    logger.warning(
        "iap(google): acknowledge 미구현 — 자격증명 대기 kind=%s product=%s", kind, product_id,
    )
    return False
