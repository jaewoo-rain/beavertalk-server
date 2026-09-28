"""IAP 영수증 검증 어댑터 — 애플/구글 + 스텁.

🧒 이 파일이 하는 일 한 줄:
    "앱이 준 영수증이 진짜인가?"를 판정한다. 앱 말을 믿지 않고 스토어에 직접 묻는 게
    IAP 백엔드의 존재 이유다(앱은 사용자 기기에서 돌아 위조가 가능하다).

⛔⛔ S1(2026-09-27, 키 도착) — _verify_apple·_verify_google·_acknowledge_google 실구현.
`settings.IAP_VERIFY_ENABLED`(기본 False)가 꺼져 있으면 여전히 스텁이다 — 이 파일이
바뀌어도 켜기 전까지 운영 동작은 그대로다(bt-back 이 Cloud Run env 로 켠다).

⛔ 스텁은 서명을 안 본다 = 아무 문자열이나 통과한다. 절대 그대로 운영에 쓰면 안 된다.

graceful 규율(R5): 키 부재·네트워크 오류는 예외를 던지지 않고 결과 객체로 돌려준다 —
호출부가 422(무효)와 503(일시 실패)을 구분해 응답할 수 있어야 하기 때문. 한쪽 플랫폼의
키가 없어도(예: 애플만 아직) 다른 플랫폼은 정상 동작해야 한다 — 그래서 실패는 플랫폼
단위로 격리한다(공용 헬퍼가 예외를 삼키고 None/False 를 돌려준다).

계약: docs/20260731_1230_IAP-API-계약서-프론트공유용.md
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional

import httpx
import jwt

from core.config import settings

logger = logging.getLogger(__name__)

Platform = Literal["ios", "android"]
# ⭐ §2(2026-09-28) — "bundle"(캐릭터 묶음)도 스토어 입장에선 캐릭터와 똑같은
#   비소모성 일회성 상품이다. 아래 _verify_google/_verify_apple/_acknowledge_google
#   은 전부 `kind == "subscription"` 만 따로 갈라내고 나머지는 한 분기로 처리하므로
#   "bundle" 은 그 분기를 그대로 탄다(새 코드 불필요) — 타입에만 추가한다.
Kind = Literal["character", "subscription", "bundle"]

_GOOGLE_ANDROIDPUBLISHER_BASE = "https://androidpublisher.googleapis.com/androidpublisher/v3"
_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
_GOOGLE_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
_APPLE_AUD = "appstoreconnect-v1"


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
    expires_at: Optional[datetime] = None
    # 스텁으로 통과했는지(로그·응답 진단용).
    stubbed: bool = False
    # ⭐ S1(2026-09-27) — 스토어가 직접 확인해 준 테스트/샌드박스 여부(라이선스 테스트
    #   계정·StoreKit 샌드박스). 클라이언트 self-report(is_sandbox 인자)와 OR 로 합쳐
    #   IapReceipt.is_sandbox 에 반영한다 — 지급은 그대로 하되(테스터도 기능을 써봐야
    #   한다) 나중에 만들 매출 집계에서 이 값으로 걸러낸다(누구나 가입 가능한 라이선스
    #   테스트 그룹으로 공짜 Premium 을 "매출"로 잡는 사고 방지).
    store_confirmed_test: bool = False
    # ⭐ §22-⑤⑦(2026-09-28) — 이 구독 건이 체험(무료/도입) 오퍼인가. 구독일 때만
    #   의미 있다(캐릭터는 항상 False). Google 은 offerId=='trial-7d', Apple 은
    #   offerType==1(Introductory) — 하류(subscribe.is_trial)가 그대로 저장한다.
    is_trial: bool = False
    # ⭐⭐ §22-⑥(2026-09-28) — 결제 내역 기록용. **딱 이 3개만**(요청서 지시) — 필드를
    #   더 늘리지 마라. 출처(WebSearch 로 공식 문서 확인, 2026-09-28):
    #     - Apple(JWSTransactionDecodedPayload): `price`(밀리단위, 1000=1단위) ·
    #       `currency`(ISO 4217) — 구독·캐릭터·묶음(비소모성) 전부 이 필드가 있다.
    #       https://developer.apple.com/documentation/appstoreserverapi/price
    #     - Google 구독(subscriptionsv2.get 의 lineItems[].autoRenewingPlan.
    #       recurringPrice): {currencyCode, units, nanos}(Money 타입) ·
    #       lineItems[].latestSuccessfulOrderId.
    #     - ⛔ Google 캐릭터·묶음(purchases.products.get 의 ProductPurchase)은
    #       **가격 필드가 원천에 없다**(공식 필드 목록 확인 — orderId 뿐). 그래서
    #       Google 캐릭터·묶음 구매는 price_amount_micros/price_currency 가
    #       구조적으로 항상 None 이다(버그 아님, 스토어가 안 준다).
    #   단위는 전부 **micros**(1,000,000=1단위)로 통일해 돌려준다 — Apple 의
    #   밀리단위·Google 의 units+nanos 는 각 _verify_* 에서 변환한다.
    price_amount_micros: Optional[int] = None
    price_currency: Optional[str] = None  # ISO 4217, 예: "USD"
    order_id: Optional[str] = None  # Google orderId / Apple transactionId(주문 개념의 등가물)


def verify(
    platform: Platform,
    kind: Kind,
    product_id: str,
    transaction_id: str,
    purchase_token: str,
    is_sandbox: bool = False,
) -> VerifyResult:
    """영수증 1건을 검증한다.

    실검증이 켜져 있으면(IAP_VERIFY_ENABLED) 플랫폼별 어댑터로, 아니면 스텁으로 간다.
    kind 로 구독(subscriptionsv2/Get All Subscription Statuses)과 캐릭터(products/
    Get Transaction Info)의 엔드포인트가 갈린다 — 두 플랫폼 다 마찬가지다.
    """
    if settings.IAP_VERIFY_ENABLED:
        result = (
            _verify_apple(kind, product_id, transaction_id, purchase_token, is_sandbox)
            if platform == "ios"
            else _verify_google(kind, product_id, transaction_id, purchase_token, is_sandbox)
        )
        # ⭐ bt-back 조건①(2026-09-27) — 클라 self-report 와 스토어 실측이 어긋나면
        #   로그를 남긴다. 클라=False·스토어=True(라이선스 테스트 계정으로 클라가
        #   그 사실을 안 보냄)를 잡아내는 게 목적 — is_sandbox 최종값(IapReceipt 에
        #   쓸 값)은 OR 이 안전한 방향이지만, 어긋난 사실 자체는 조용히 넘기지 않는다.
        if result.ok and is_sandbox != result.store_confirmed_test:
            logger.warning(
                "iap: sandbox 불일치 platform=%s product=%s 클라=%s 스토어=%s",
                platform, product_id, is_sandbox, result.store_confirmed_test,
            )
        return result

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


def _parse_rfc3339(value: Optional[str]) -> Optional[datetime]:
    """구글 응답의 RFC3339 타임스탬프("...Z" 포함)를 UTC datetime 으로.

    파이썬 버전에 따라 fromisoformat 이 "Z" 접미사를 못 받을 수 있어(3.11 이전) 미리
    "+00:00" 으로 바꾼다 — 배포 파이썬 버전에 기대지 않는다.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("iap: RFC3339 파싱 실패 value=%s", value)
        return None


def _google_money_to_micros(money: Optional[dict]) -> Optional[int]:
    """구글 Money 타입({currencyCode, units, nanos}) → micros(1,000,000=1단위).

    units 는 int64 호환을 위해 문자열로 올 수 있어 int() 로 강제한다. 파싱이
    안 되면(예상 밖 모양) 조용히 None — 결제 기록 자체를 막을 이유는 아니다(R5).
    """
    if not money:
        return None
    try:
        units = int(money.get("units", 0) or 0)
        nanos = int(money.get("nanos", 0) or 0)
    except (TypeError, ValueError):
        return None
    return units * 1_000_000 + nanos // 1_000


# ═══════════════════════════════════════════════════════════════════════ #
# 구글 — Play Developer API (androidpublisher v3)
# ═══════════════════════════════════════════════════════════════════════ #
# access token 캐시 — 매 요청마다 새로 발급하면 구글 토큰 엔드포인트에 불필요한
# 부하를 준다. 모듈 전역 dict 하나로 충분하다(단일 서비스계정, 프로세스당 1개).
_google_token_cache: dict = {}


def _google_access_token() -> Optional[str]:
    """서비스계정 JSON 키로 OAuth2 JWT-bearer 플로우를 직접 돈다(만료 60초 전 갱신).

    google-auth 의 requests 기반 transport 를 안 쓴다 — 그러면 `requests` 를 새
    의존성으로 추가해야 하는데, 이 저장소는 이미 httpx 를 쓴다(core/apns.py 등과
    같은 선택). pyjwt(RS256, 이미 `pyjwt[crypto]` 의존성)로 assertion 만 만들면
    충분해서 새 의존성이 필요 없다.
    """
    now = time.time()
    cached = _google_token_cache.get("access_token")
    if cached and _google_token_cache.get("expires_at", 0) - 60 > now:
        return cached

    key_path = settings.PLAY_RECEIPT_SA_KEY_PATH
    if not key_path:
        logger.warning("iap(google): PLAY_RECEIPT_SA_KEY_PATH 미설정 → 검증 불가")
        return None

    import json

    try:
        with open(key_path, "r", encoding="utf-8") as f:
            sa = json.load(f)
        assertion = jwt.encode(
            {
                "iss": sa["client_email"],
                "scope": _GOOGLE_SCOPE,
                "aud": sa.get("token_uri", _GOOGLE_TOKEN_URI),
                "iat": int(now),
                "exp": int(now) + 3600,
            },
            sa["private_key"],
            algorithm="RS256",
        )
        resp = httpx.post(
            sa.get("token_uri", _GOOGLE_TOKEN_URI),
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        body = resp.json()
        _google_token_cache["access_token"] = body["access_token"]
        _google_token_cache["expires_at"] = now + body.get("expires_in", 3600)
        return body["access_token"]
    except Exception as exc:  # noqa: BLE001 - 키 부재·파싱·네트워크 모두 검증 불가로
        logger.warning("iap(google): access token 발급 실패 — %s", exc)
        return None


def _verify_google(
    kind: Kind, product_id: str, transaction_id: str, purchase_token: str, is_sandbox: bool
) -> VerifyResult:
    """Play Developer API 로 실제 검증.

    구현 시(완료): `purchases.subscriptionsv2.get`(구독) / `purchases.products.get`
    (캐릭터, 일회성)로 purchaseToken 을 조회한다.
    유효 판정(구독): `subscriptionState` 가 ACTIVE 또는 IN_GRACE_PERIOD 이고,
    요청한 product_id 가 lineItems 중 하나와 일치해야 한다(같은 토큰이 다른 상품을
    가리키는 요청을 막는다). 만료는 그 lineItem 의 expiryTime.
    유효 판정(캐릭터): `purchaseState == 0`(구매 완료, 취소·대기 아님) + productId 일치.

    ⭐ 윈백(winback) 오퍼는 `lineItems[].offerDetails.offerId`(예: "winback-50-1m")로
    구분 가능하다 — 요청서 §16, 지금은 지급 로직을 안 가르고 **로그로만 기록**한다
    (선택 항목, 나중에 필요해지면 이 로그를 근거로 집계 붙인다).

    ⛔ 검증 후 **반드시 acknowledge** 를 보내야 한다(acknowledge() 참조 — 지급이
       끝난 뒤에만 부른다). 3일 안에 안 하면 구글이 자동 환불하고, 돈은 돌아가는데
       지급은 남는 사고가 난다(애플엔 없는 절차 — acknowledge() 의 애플 분기 주석 참조).
    """
    token = _google_access_token()
    if token is None:
        return VerifyResult(ok=False, reason="unavailable")

    headers = {"Authorization": f"Bearer {token}"}
    pkg = settings.IAP_ANDROID_PACKAGE_NAME
    try:
        if kind == "subscription":
            url = (
                f"{_GOOGLE_ANDROIDPUBLISHER_BASE}/applications/{pkg}"
                f"/purchases/subscriptionsv2/tokens/{purchase_token}"
            )
            resp = httpx.get(url, headers=headers, timeout=10.0)
            if resp.status_code in (400, 404):
                return VerifyResult(ok=False, reason="invalid")
            resp.raise_for_status()
            body = resp.json()

            state = body.get("subscriptionState")
            line_items = body.get("lineItems") or []
            match = next((li for li in line_items if li.get("productId") == product_id), None)
            if match is None or state not in (
                "SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
            ):
                return VerifyResult(ok=False, reason="invalid")

            offer_id = (match.get("offerDetails") or {}).get("offerId")
            if offer_id:
                logger.info(
                    "iap(google): 윈백/오퍼 감지 product=%s offer=%s(기록만, 지급 로직 무관)",
                    product_id, offer_id,
                )
            # ⭐⭐ §22-⑥ — 가격은 autoRenewingPlan.recurringPrice 에 있다(prepaidPlan
            #   은 확인 못 했다 — 그 경우 조용히 None, 결제 기록 자체를 막지 않는다).
            #   주문 ID 는 lineItem 별 latestSuccessfulOrderId(공식 필드, WebSearch로
            #   확인 — SubscriptionPurchaseLineItem.latest_successful_order_id).
            recurring_price = (match.get("autoRenewingPlan") or {}).get("recurringPrice")
            return VerifyResult(
                ok=True,
                transaction_id=transaction_id,
                expires_at=_parse_rfc3339(match.get("expiryTime")),
                store_confirmed_test="testPurchase" in body,
                # ⭐ §22-⑤⑦ — 체험 오퍼 코드. 위 offer_id 로그와 같은 자리(응답을
                #   이미 파싱해 손에 든 상태)에서 판정만 한 줄 추가한다.
                is_trial=offer_id == "trial-7d",
                price_amount_micros=_google_money_to_micros(recurring_price),
                price_currency=(recurring_price or {}).get("currencyCode"),
                order_id=match.get("latestSuccessfulOrderId"),
            )

        url = (
            f"{_GOOGLE_ANDROIDPUBLISHER_BASE}/applications/{pkg}"
            f"/purchases/products/{product_id}/tokens/{purchase_token}"
        )
        resp = httpx.get(url, headers=headers, timeout=10.0)
        if resp.status_code in (400, 404):
            return VerifyResult(ok=False, reason="invalid")
        resp.raise_for_status()
        body = resp.json()
        if body.get("purchaseState") != 0:  # 0=구매완료(취소·대기 아님)
            return VerifyResult(ok=False, reason="invalid")
        return VerifyResult(
            ok=True,
            transaction_id=transaction_id,
            store_confirmed_test=body.get("purchaseType") == 0,  # 0=라이선스 테스트 계정
            # ⛔⛔ §22-⑥ — ProductPurchase(캐릭터·묶음, 비소모성)에는 가격 필드가
            #   **원천에 없다**(공식 필드 목록 확인: kind·purchaseTimeMillis·
            #   purchaseState·consumptionState·developerPayload·orderId·purchaseType·
            #   acknowledgementState·purchaseToken·productId·quantity·
            #   obfuscatedExternalAccountId·obfuscatedExternalProfileId·regionCode·
            #   refundableQuantity 뿐). price_amount_micros/price_currency 가 항상
            #   None 인 건 버그가 아니라 스토어가 원래 안 준다 — "채워지게 고쳐라"로
            #   되돌리지 마라. orderId 는 있다.
            order_id=body.get("orderId"),
        )
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "iap(google): 검증 실패 status=%s product=%s — %s",
            exc.response.status_code, product_id, exc,
        )
        return VerifyResult(ok=False, reason="unavailable")
    except Exception as exc:  # noqa: BLE001 - 네트워크·파싱 등 모두 일시 실패로
        logger.warning("iap(google): 검증 중 예외 product=%s — %s", product_id, exc)
        return VerifyResult(ok=False, reason="unavailable")


def _acknowledge_google(kind: Kind, purchase_token: str, product_id: str) -> bool:
    """Google Play acknowledge — **v1**(subscriptionsv2 조회와는 별개 API 계열).

    kind=="subscription" 이면 `purchases.subscriptions.acknowledge`(경로의
    `{subscriptionId}` 자리엔 구독 상품의 productId 를 넣는다 — 구매별 ID 가 아니다),
    kind=="character" 면 `purchases.products.acknowledge`.
    """
    token = _google_access_token()
    if token is None:
        return False
    pkg = settings.IAP_ANDROID_PACKAGE_NAME
    segment = "subscriptions" if kind == "subscription" else "products"
    url = (
        f"{_GOOGLE_ANDROIDPUBLISHER_BASE}/applications/{pkg}"
        f"/purchases/{segment}/{product_id}/tokens/{purchase_token}:acknowledge"
    )
    try:
        resp = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, json={}, timeout=10.0)
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 - 네트워크·권한 등 — 실패해도 지급은 유지(R5)
        logger.warning(
            "iap(google): acknowledge 실패 kind=%s product=%s — %s", kind, product_id, exc,
        )
        return False


# ═══════════════════════════════════════════════════════════════════════ #
# 애플 — App Store Server API
# ═══════════════════════════════════════════════════════════════════════ #
def _apple_jwt() -> Optional[str]:
    """App Store Server API 인증용 ES256 JWT(5분 만료 — 애플 공식 라이브러리와 동일 값).

    헤더 kid=Key ID, 클레임 iss=Issuer ID·aud="appstoreconnect-v1"·bid=번들ID·exp.
    (참고: apple/app-store-server-library-python 의 _generate_token 구현 그대로 —
    iat·nonce 는 안 넣는다.)
    """
    key_path = settings.APPSTORE_IAP_KEY_PATH
    if not key_path:
        logger.warning("iap(apple): APPSTORE_IAP_KEY_PATH 미설정 → 검증 불가")
        return None
    # ⛔⛔ S1 정정(2026-09-27, bt-back QA) — Issuer ID·Key ID 는 기본값이 없다(공개
    #   저장소에 식별자를 안 남긴다). 둘 중 하나라도 없으면 **조용히 넘어가지 않는다**
    #   — jwt.encode 는 kid=None 을 그냥 받아 "유효해 보이는" JWT 를 만들어버려서,
    #   이 체크가 없으면 애플이 401 을 줄 때까지 원인이 안 보인다(LIVE_MODEL_VOICE_VERTEX
    #   가 비어 2주간 조용히 느린 경로로 돈 사고와 같은 모양 — 이번엔 로그로 못박는다).
    if not settings.IAP_APPLE_ISSUER_ID or not settings.IAP_APPLE_KEY_ID:
        logger.warning("iap(apple): IAP_APPLE_ISSUER_ID/IAP_APPLE_KEY_ID 미설정 → 검증 불가")
        return None
    try:
        with open(key_path, "r", encoding="utf-8") as f:
            private_key = f.read()
        now = int(time.time())
        return jwt.encode(
            {
                "iss": settings.IAP_APPLE_ISSUER_ID,
                "aud": _APPLE_AUD,
                "bid": settings.IAP_APPLE_BUNDLE_ID,
                "exp": now + 300,
            },
            private_key,
            algorithm="ES256",
            headers={"kid": settings.IAP_APPLE_KEY_ID},
        )
    except Exception as exc:  # noqa: BLE001 - 키 부재·파싱 등 모두 검증 불가로
        logger.warning("iap(apple): JWT 서명 실패 — %s", exc)
        return None


def _decode_apple_transaction(signed: Optional[str]) -> Optional[dict]:
    """`signedTransactionInfo`(JWS)를 **서명 검증 없이** 디코드한다.

    ⚠⚠ 의도적 설계 — 이 값은 우리가 App Store Server API 에 **서버-서버로 직접**
    (TLS 로 인증된 채널) 물어봐서 받은 응답이다. StoreKit2 가 클라이언트에 직접 주는
    서명 트랜잭션(위조 가능한 기기에서 온 것이라 서명 검증이 필수인 경로)과 다르다
    — 우리는 애플 서버에서 곧바로 받았으므로 TLS 자체가 이미 신뢰 경계다.
    인증서 체인(x5c) 검증까지 하려면 애플 루트/중간 인증서 검증 로직이 따로 필요한데
    (공식 SignedDataVerifier 가 하는 일) 지금 범위에선 과설계로 판단했다 — 이 판단은
    애플 키가 실전에서 검증되면 재검토해라.

    ⛔⛔⛔ 이 면제는 **이 호출 경로에만** 적용된다(2026-09-27, bt-back QA) — "우리가
    애플에 직접 물어서 받은 응답"이라는 전제 그 하나 때문이다. **App Store Server
    Notifications(V2, 애플이 우리 엔드포인트로 POST 하는 수신 웹훅)를 구현할 때 이
    함수를 재사용하거나 이 면제를 그쪽에 복사하지 마라** — 그 URL 은 공개돼 있어
    누구나 위조한 JWS 를 밀어넣을 수 있다(우리가 물어본 게 아니라 상대가 보낸 것 —
    신뢰 경계가 반대다). 알림 수신 경로는 **반드시 서명(x5c 인증서 체인)을 검증**해야
    한다 — 여기서 벗어난 판단을 그대로 옮기면 그게 인증 우회가 된다.
    """
    if not signed:
        return None
    try:
        return jwt.decode(signed, options={"verify_signature": False})
    except Exception as exc:  # noqa: BLE001 - 디코드 실패는 "정보 없음"으로
        logger.warning("iap(apple): signedTransactionInfo 디코드 실패 — %s", exc)
        return None


def _apple_price_micros(info: dict) -> Optional[int]:
    """§22-⑥ — JWSTransactionDecodedPayload.price(밀리단위, 1000=1단위) → micros.

    price·currency 는 Apple 공식 문서로 확인된 필드다(구독·비소모성 공통) —
    https://developer.apple.com/documentation/appstoreserverapi/price
    """
    price = info.get("price")
    if price is None:
        return None
    try:
        return int(price) * 1_000
    except (TypeError, ValueError):
        return None


def _is_apple_sandbox_transaction(info: dict) -> bool:
    """⭐ bt-back 조건③(2026-09-27) — `_decode_apple_transaction` 이 디코드한
    JWSTransactionDecodedPayload(`signedTransactionInfo`)의 **`environment`** 필드가
    출처다("Sandbox" | "Production", apple/app-store-server-library-python 의
    JWSTransactionDecodedPayload 모델 확인). 애플이 대소문자를 바꿔 내려보낸 선례가
    흔하다고 해서 대소문자 무시로 비교한다 — 원문 그대로 비교하면 조용히 항상
    False(=운영으로 오분류)가 될 수 있다.
    """
    return str(info.get("environment") or "").strip().lower() == "sandbox"


def _verify_apple(
    kind: Kind, product_id: str, transaction_id: str, purchase_token: str, is_sandbox: bool
) -> VerifyResult:
    """App Store Server API 로 실제 검증.

    구독: 「Get All Subscription Statuses」(`GET /inApps/v1/subscriptions/{id}`) —
    그 회원 소유 아무 거래 id 하나로 **전체** 구독군의 최신 상태를 돌려주므로, 응답
    안에서 요청한 product_id 와 일치하는 lastTransaction 을 찾아 그 status 를 본다.
    캐릭터(일회성): 「Get Transaction Info」(`GET /inApps/v1/transactions/{id}`).

    transaction_id 는 IapReceipt 계약상 iOS 의 originalTransactionId(API 경로에
    그대로 쓸 수 있다 — "may be an original transaction identifier").
    purchase_token(StoreKit2 `Transaction.jwsRepresentation`)은 여기서 안 쓴다 —
    서버가 애플에 직접 물어보므로 클라이언트가 보낸 서명 값을 우리가 검증할 필요가
    없다(위 _decode_apple_transaction 의 판단과 같은 이유).

    ⚠ 애플엔 acknowledge 류 절차가 **없는 것으로 보인다**(원문 명시적 부정문은
    확인 못 함 — acknowledge() 의 애플 분기 주석 참조. 지우지 마라).
    """
    token = _apple_jwt()
    if token is None:
        return VerifyResult(ok=False, reason="unavailable")

    base = "https://api.storekit-sandbox.apple.com" if is_sandbox else "https://api.storekit.apple.com"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        if kind == "subscription":
            resp = httpx.get(
                f"{base}/inApps/v1/subscriptions/{transaction_id}", headers=headers, timeout=10.0,
            )
            if resp.status_code == 404:
                return VerifyResult(ok=False, reason="invalid")
            resp.raise_for_status()
            body = resp.json()

            matched_tx = matched_info = None
            for group in body.get("data") or []:
                for tx in group.get("lastTransactions") or []:
                    info = _decode_apple_transaction(tx.get("signedTransactionInfo"))
                    if info and info.get("productId") == product_id:
                        matched_tx, matched_info = tx, info
                        break
                if matched_tx is not None:
                    break
            if matched_tx is None:
                return VerifyResult(ok=False, reason="invalid")
            # status: 1=ACTIVE, 4=BILLING_GRACE_PERIOD 만 유효로 본다(2=EXPIRED·
            #   3=BILLING_RETRY·5=REVOKED 는 무효 — retry 를 유효로 볼지는 요청서에
            #   명시가 없어 보수적으로 무효 처리했다).
            if matched_tx.get("status") not in (1, 4):
                return VerifyResult(ok=False, reason="invalid")
            expires_ms = matched_info.get("expiresDate")
            expires_at = (
                datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc) if expires_ms else None
            )
            return VerifyResult(
                ok=True,
                transaction_id=matched_info.get("originalTransactionId") or transaction_id,
                expires_at=expires_at,
                store_confirmed_test=_is_apple_sandbox_transaction(matched_info),
                # ⭐ §22-⑤⑦ — offerType 1=Introductory(체험/도입가), 2=Promotional,
                #   3=Offer Code. JWSTransactionDecodedPayload 필드(apple/app-store-
                #   server-library-python 모델 확인).
                is_trial=matched_info.get("offerType") == 1,
                price_amount_micros=_apple_price_micros(matched_info),
                price_currency=matched_info.get("currency"),
                # ⭐ §22-⑥ — Apple 엔 별도 "주문 ID" 개념이 없다. transactionId 가
                #   그 등가물이다(이 거래 자체를 가리키는 스토어 발급 식별자).
                order_id=matched_info.get("transactionId"),
            )

        resp = httpx.get(
            f"{base}/inApps/v1/transactions/{transaction_id}", headers=headers, timeout=10.0,
        )
        if resp.status_code == 404:
            return VerifyResult(ok=False, reason="invalid")
        resp.raise_for_status()
        info = _decode_apple_transaction(resp.json().get("signedTransactionInfo"))
        if info is None or info.get("productId") != product_id:
            return VerifyResult(ok=False, reason="invalid")
        return VerifyResult(
            ok=True,
            transaction_id=info.get("originalTransactionId") or transaction_id,
            store_confirmed_test=_is_apple_sandbox_transaction(info),
            price_amount_micros=_apple_price_micros(info),
            price_currency=info.get("currency"),
            order_id=info.get("transactionId"),
        )
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "iap(apple): 검증 실패 status=%s product=%s — %s",
            exc.response.status_code, product_id, exc,
        )
        return VerifyResult(ok=False, reason="unavailable")
    except Exception as exc:  # noqa: BLE001 - 네트워크·파싱 등 모두 일시 실패로
        logger.warning("iap(apple): 검증 중 예외 product=%s — %s", product_id, exc)
        return VerifyResult(ok=False, reason="unavailable")


def acknowledge(
    platform: Platform, kind: Kind, purchase_token: str, product_id: str
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
      않는다.** 요청서(2026-09-26)도 구글만 3일 자동환불을 적었다 — 애플 쪽 원문은
      여전히 미확인이다. **이 판단·이 문단을 지우지 마라.**
    """
    if platform != "android":
        logger.info("iap(apple): acknowledge 미대상(애플엔 이 절차가 없는 것으로 보임, 위 docstring 참조)")
        return True
    if not settings.IAP_VERIFY_ENABLED:
        logger.info("iap(google): 검증 비활성 상태 → acknowledge 스킵(스텁 지급)")
        return True
    return _acknowledge_google(kind, purchase_token, product_id)
