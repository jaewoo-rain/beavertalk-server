"""S1(2026-09-27, 키 도착) — core/iap.py 실검증(구글·애플) 단위 시험.

외부 의존 0 — httpx.get/post 를 직접 monkeypatch 하고(이 저장소에 fixture-server 류
HTTP mock 라이브러리가 없어 최소한으로), 서명 키는 매 세션 한 번 생성한 진짜 EC/RSA
키(테스트 전용, 운영과 무관 — 시크릿 아님)를 임시 파일로 써서 pyjwt 가 실제로 서명·
디코드하게 한다(가짜 문자열 키로는 jwt.encode 자체가 실패한다).

⛔ 이 파일에 실제 서비스계정·애플 키 값을 절대 넣지 않는다 — 전부 이 세션이 새로
생성한 무의미한 테스트 키다.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from core import iap
from core.config import settings as app_settings


# --------------------------------------------------------------------------- #
# 테스트 전용 키 — 세션에 한 번만 생성(RSA 는 느리다)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def ec_private_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture(scope="session")
def rsa_private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture()
def apple_key_path(tmp_path, ec_private_pem, monkeypatch):
    p = tmp_path / "appstore-iap-key.p8"
    p.write_text(ec_private_pem, encoding="utf-8")
    monkeypatch.setattr(app_settings, "APPSTORE_IAP_KEY_PATH", str(p))
    # ⛔⛔ S1 정정(2026-09-27, bt-back QA) — Issuer ID·Key ID 는 기본값이 없다(공개
    #   저장소 보호). 시험은 진짜 값이 아니라 가짜 식별자로 채운다.
    monkeypatch.setattr(app_settings, "IAP_APPLE_ISSUER_ID", "test-issuer-id")
    monkeypatch.setattr(app_settings, "IAP_APPLE_KEY_ID", "TESTKEYID99")
    return str(p)


@pytest.fixture()
def google_sa_key_path(tmp_path, rsa_private_pem, monkeypatch):
    sa = {
        "client_email": "sa@example.invalid",  # 가짜 — 실제 서비스계정 주소를 시험에 안 둔다
        "private_key": rsa_private_pem,
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    p = tmp_path / "play-receipt-sa-key.json"
    p.write_text(json.dumps(sa), encoding="utf-8")
    monkeypatch.setattr(app_settings, "PLAY_RECEIPT_SA_KEY_PATH", str(p))
    return str(p)


@pytest.fixture(autouse=True)
def _reset_google_token_cache():
    """모듈 전역 캐시가 시험 간에 새는 것을 막는다."""
    iap._google_token_cache.clear()
    iap._google_catalog_cache.clear()
    yield
    iap._google_token_cache.clear()
    iap._google_catalog_cache.clear()


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("GET", "https://example.test")
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=req, response=self,  # type: ignore[arg-type]
            )


def _fake_google_token_post(*, access_token="fake-access-token"):
    def _post(url, **kwargs):
        assert url == "https://oauth2.googleapis.com/token"
        assert kwargs["data"]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
        # assertion 이 실제로 서명된 JWT 인지 확인(그냥 아무 문자열이 아니라)
        jwt.decode(kwargs["data"]["assertion"], options={"verify_signature": False})
        return _FakeResponse(200, {"access_token": access_token, "expires_in": 3600})
    return _post


# --------------------------------------------------------------------------- #
# 1) 구글 access token — 서명·캐시·부재
# --------------------------------------------------------------------------- #
def test_google_access_token_mints_a_real_signed_assertion(google_sa_key_path, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    tok = iap._google_access_token()
    assert tok == "fake-access-token"


def test_google_access_token_is_cached_across_calls(google_sa_key_path, monkeypatch):
    calls = []

    def _post(url, **kwargs):
        calls.append(1)
        return _FakeResponse(200, {"access_token": "tok-1", "expires_in": 3600})

    monkeypatch.setattr(httpx, "post", _post)
    assert iap._google_access_token() == "tok-1"
    assert iap._google_access_token() == "tok-1"
    assert len(calls) == 1  # 두 번째는 캐시에서


def test_google_access_token_none_when_key_path_unset(monkeypatch):
    monkeypatch.setattr(app_settings, "PLAY_RECEIPT_SA_KEY_PATH", None)
    assert iap._google_access_token() is None


# --------------------------------------------------------------------------- #
# 2) 구글 구독 검증 — 상태·만료·상품일치
# --------------------------------------------------------------------------- #
def _sub_body(state="SUBSCRIPTION_STATE_ACTIVE", product="bt_pro_monthly",
              expiry="2099-01-01T00:00:00Z", extra_line_item=None, test_purchase=False):
    line_item = {"productId": product, "expiryTime": expiry}
    if extra_line_item:
        line_item.update(extra_line_item)
    body = {"subscriptionState": state, "lineItems": [line_item]}
    if test_purchase:
        body["testPurchase"] = {}
    return body


def test_verify_google_subscription_active_is_ok(google_sa_key_path, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, _sub_body()))

    result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)

    assert result.ok is True
    assert result.expires_at == datetime(2099, 1, 1, tzinfo=timezone.utc)
    assert result.store_confirmed_test is False


def test_verify_google_subscription_grace_period_is_ok(google_sa_key_path, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    body = _sub_body(state="SUBSCRIPTION_STATE_IN_GRACE_PERIOD")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)
    assert result.ok is True


@pytest.mark.parametrize("state", [
    "SUBSCRIPTION_STATE_EXPIRED", "SUBSCRIPTION_STATE_CANCELED",
    "SUBSCRIPTION_STATE_ON_HOLD", "SUBSCRIPTION_STATE_PAUSED",
])
def test_verify_google_subscription_inactive_states_are_invalid(state, google_sa_key_path, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    body = _sub_body(state=state)
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)
    assert result.ok is False and result.reason == "invalid"


def test_verify_google_subscription_wrong_product_is_invalid(google_sa_key_path, monkeypatch):
    """같은 토큰인데 다른 product_id 를 주장하면(위조 시도) 무효."""
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    body = _sub_body(product="bt_pro_monthly")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    result = iap._verify_google("subscription", "bt_max_monthly", "tx-1", "ptok", False)
    assert result.ok is False and result.reason == "invalid"


def test_verify_google_subscription_test_purchase_flag_detected(google_sa_key_path, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    body = _sub_body(test_purchase=True)
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)
    assert result.ok is True
    assert result.store_confirmed_test is True


def test_verify_google_subscription_offer_id_does_not_change_grant_result(google_sa_key_path, monkeypatch):
    """윈백 오퍼가 있어도(§16) 지급 판정은 그대로다 — 기록만 한다."""
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    body = _sub_body(extra_line_item={"offerDetails": {"offerId": "winback-50-1m"}})
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)
    assert result.ok is True


def test_verify_google_subscription_offer_phase_base_price_is_not_trial(google_sa_key_path, monkeypatch):
    """⭐⭐ §26-③(2026-09-29, bt-back 실기기 회귀·실측) — 체험이 끝나 유료로 전환된
    구독의 실제 응답 모양(offerDetails.offerId="trial-7d" 는 남아 있지만 offerPhase
    는 basePrice). 옛 판정(offerId 존재만 봄)은 여기서 영원히 True 였다."""
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    body = _sub_body(extra_line_item={
        "offerDetails": {"basePlanId": "monthly", "offerId": "trial-7d"},
        "offerPhase": {"basePrice": {}},
    })
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)
    assert result.ok is True
    assert result.is_trial is False


def test_verify_google_subscription_offer_phase_free_trial_is_trial(google_sa_key_path, monkeypatch):
    """⭐⭐ §26-③ 정정(bt-back, androidpublisher v3 디스커버리 문서) — OfferPhase 는
    4종(freeTrial/introductoryPrice/basePrice/prorationPeriod)이다. freeTrial 키가
    와야만 체험이다(양성 판정)."""
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    body = _sub_body(extra_line_item={
        "offerDetails": {"basePlanId": "monthly", "offerId": "trial-7d"},
        "offerPhase": {"freeTrial": {}},
    })
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)
    assert result.ok is True
    assert result.is_trial is True


def test_verify_google_subscription_offer_phase_introductory_price_is_not_trial(google_sa_key_path, monkeypatch):
    """⛔⛔ §26-③ 정정(bt-back) — introductoryPrice(도입가)는 **돈을 낸다**, 체험이
    아니다. 옛 음성 판정("basePrice 아니면 체험")이면 여기서 오판했다."""
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    body = _sub_body(extra_line_item={"offerPhase": {"introductoryPrice": {}}})
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)
    assert result.ok is True
    assert result.is_trial is False


def test_verify_google_subscription_offer_phase_proration_period_is_not_trial(google_sa_key_path, monkeypatch):
    """⛔⛔ §26-③ 정정(bt-back) — prorationPeriod(플랜 변경 일할 정산)는 이론이
    아니다: 앱의 안드로이드 월↔연 전환(CHARGE_FULL_PRICE, 남은 월간 가치는 기간
    연장)이 정확히 이 단계를 만든다. 옛 음성 판정이면 이 전환 회원이 Trial
    배지를 보게 된다 — 이게 지금 고치는 그 버그였다."""
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    body = _sub_body(extra_line_item={"offerPhase": {"prorationPeriod": {}}})
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)
    assert result.ok is True
    assert result.is_trial is False


def test_verify_google_subscription_offer_phase_absent_is_unknown(google_sa_key_path, monkeypatch):
    """offerPhase 자체가 없는(구 API 등) 응답에서는 판단하지 않는다 — None(모름).
    하류가 이 None 을 보고 기존 저장값을 그대로 둔다."""
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    body = _sub_body(extra_line_item={"offerDetails": {"offerId": "trial-7d"}})  # offerPhase 없음
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)
    assert result.ok is True
    assert result.is_trial is None


def test_verify_google_subscription_400_is_invalid_not_unavailable(google_sa_key_path, monkeypatch):
    """bt-back 실측(2026-09-27) — 가짜 토큰이 404→400 Invalid Value 로 바뀜(권한 정상)."""
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(400, {}))

    result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)
    assert result.ok is False and result.reason == "invalid"


def test_verify_google_server_error_is_unavailable(google_sa_key_path, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(500, {}))

    result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)
    assert result.ok is False and result.reason == "unavailable"


def test_verify_google_missing_sa_key_is_unavailable(monkeypatch):
    monkeypatch.setattr(app_settings, "PLAY_RECEIPT_SA_KEY_PATH", None)
    result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)
    assert result.ok is False and result.reason == "unavailable"


# --------------------------------------------------------------------------- #
# 3) 구글 캐릭터(일회성) 검증 — ⛔ purchaseType 부재 vs 0 함정(bt-back 조건②)
# --------------------------------------------------------------------------- #
def test_verify_google_character_purchased_is_ok(google_sa_key_path, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, {"purchaseState": 0}))

    result = iap._verify_google("character", "bt_character_bibi", "tx-1", "ptok", False)
    assert result.ok is True


@pytest.mark.parametrize("state", [1, 2])  # 1=취소, 2=대기
def test_verify_google_character_not_purchased_is_invalid(state, google_sa_key_path, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, {"purchaseState": state}))

    result = iap._verify_google("character", "bt_character_bibi", "tx-1", "ptok", False)
    assert result.ok is False and result.reason == "invalid"


def test_verify_google_character_purchase_type_absent_is_real_purchase(google_sa_key_path, monkeypatch):
    """⛔⛔ 함정 시험 — 필드가 **없으면**(일반 구매) store_confirmed_test 는 False 여야
    한다. `int(x or 0) == 0` 처럼 짜면 None 도 0 이 되어 이 시험이 깨진다."""
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    body = {"purchaseState": 0}  # purchaseType 키 자체가 없음
    assert "purchaseType" not in body
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    result = iap._verify_google("character", "bt_character_bibi", "tx-1", "ptok", False)
    assert result.ok is True
    assert result.store_confirmed_test is False


def test_verify_google_character_purchase_type_zero_is_test_purchase(google_sa_key_path, monkeypatch):
    """같은 상황에서 필드가 **0 으로 실제 존재**하면(라이선스 테스트 계정) True 여야 한다."""
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    body = {"purchaseState": 0, "purchaseType": 0}
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    result = iap._verify_google("character", "bt_character_bibi", "tx-1", "ptok", False)
    assert result.ok is True
    assert result.store_confirmed_test is True


# --------------------------------------------------------------------------- #
# 4) 구글 acknowledge — kind 별 엔드포인트
# --------------------------------------------------------------------------- #
def test_acknowledge_google_subscription_hits_subscriptions_endpoint(google_sa_key_path, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    seen = {}

    def _ack_post(url, **kw):
        if url.endswith(":acknowledge"):
            seen["url"] = url
            return _FakeResponse(200, {})
        return _fake_google_token_post()(url, **kw)

    monkeypatch.setattr(httpx, "post", _ack_post)
    ok = iap._acknowledge_google("subscription", "ptok", "bt_pro_monthly")
    assert ok is True
    assert "/purchases/subscriptions/bt_pro_monthly/tokens/ptok:acknowledge" in seen["url"]


def test_acknowledge_google_character_hits_products_endpoint(google_sa_key_path, monkeypatch):
    def _ack_post(url, **kw):
        if url.endswith(":acknowledge"):
            return _FakeResponse(200, {})
        return _fake_google_token_post()(url, **kw)

    monkeypatch.setattr(httpx, "post", _ack_post)
    calls = []

    def _tracking_post(url, **kw):
        calls.append(url)
        return _ack_post(url, **kw)

    monkeypatch.setattr(httpx, "post", _tracking_post)
    ok = iap._acknowledge_google("character", "ptok", "bt_character_bibi")
    assert ok is True
    ack_urls = [u for u in calls if u.endswith(":acknowledge")]
    assert ack_urls and "/purchases/products/bt_character_bibi/tokens/ptok:acknowledge" in ack_urls[0]


def test_acknowledge_google_failure_returns_false_not_raise(google_sa_key_path, monkeypatch):
    def _post(url, **kw):
        if url.endswith(":acknowledge"):
            return _FakeResponse(500, {})
        return _fake_google_token_post()(url, **kw)

    monkeypatch.setattr(httpx, "post", _post)
    assert iap._acknowledge_google("subscription", "ptok", "bt_pro_monthly") is False


# --------------------------------------------------------------------------- #
# 5) 애플 JWT — 서명·부재
# --------------------------------------------------------------------------- #
def test_apple_jwt_is_signed_es256_with_kid(apple_key_path):
    token = iap._apple_jwt()
    assert token is not None
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == app_settings.IAP_APPLE_KEY_ID
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == app_settings.IAP_APPLE_ISSUER_ID
    assert claims["aud"] == "appstoreconnect-v1"
    assert claims["bid"] == app_settings.IAP_APPLE_BUNDLE_ID


def test_apple_jwt_none_when_key_path_unset(monkeypatch):
    monkeypatch.setattr(app_settings, "APPSTORE_IAP_KEY_PATH", None)
    assert iap._apple_jwt() is None


def test_apple_jwt_none_and_warns_when_issuer_id_missing(apple_key_path, monkeypatch, caplog):
    """⛔⛔ S1 정정(2026-09-27) — Issuer ID·Key ID 는 이제 기본값이 없다. 미설정을
    조용히 넘기지 않는지(경고 로그 + None) 못박는다 — LIVE_MODEL_VOICE_VERTEX 류
    「비어서 2주간 조용히 돈」 사고를 반복하지 않는다."""
    monkeypatch.setattr(app_settings, "IAP_APPLE_ISSUER_ID", None)
    with caplog.at_level("WARNING", logger="core.iap"):
        assert iap._apple_jwt() is None
    assert any("IAP_APPLE_ISSUER_ID" in r.message for r in caplog.records)


def test_apple_jwt_none_and_warns_when_key_id_missing(apple_key_path, monkeypatch, caplog):
    monkeypatch.setattr(app_settings, "IAP_APPLE_KEY_ID", None)
    with caplog.at_level("WARNING", logger="core.iap"):
        assert iap._apple_jwt() is None
    assert any("IAP_APPLE_KEY_ID" in r.message for r in caplog.records)


def test_verify_apple_unavailable_when_issuer_id_missing(apple_key_path, monkeypatch):
    """_verify_apple() 까지 전체 경로로 — 조용히 통과가 아니라 503 계열로 떨어진다."""
    monkeypatch.setattr(app_settings, "IAP_APPLE_ISSUER_ID", None)
    result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", "jws", False)
    assert result.ok is False and result.reason == "unavailable"


# --------------------------------------------------------------------------- #
# 6) 애플 구독 검증 — 상태·상품일치·환경
# --------------------------------------------------------------------------- #
def _apple_tx_jws(ec_private_pem, *, product="bt_pro_monthly", original_tx="orig-1",
                   expires_ms=4102444800000, environment="Production"):
    """signedTransactionInfo 자리에 넣을 JWS(우리 검증 코드는 서명을 안 보므로 서명
    자체는 아무 개인키로 해도 되지만, 실제 JWT 형태를 만들어 디코드 경로를 진짜로 태운다."""
    return jwt.encode(
        {
            "productId": product,
            "originalTransactionId": original_tx,
            "expiresDate": expires_ms,
            "environment": environment,
        },
        ec_private_pem,
        algorithm="ES256",
    )


def _apple_status_body(ec_private_pem, *, status=1, **tx_kwargs):
    return {
        "data": [
            {"lastTransactions": [
                {"status": status, "signedTransactionInfo": _apple_tx_jws(ec_private_pem, **tx_kwargs)},
            ]},
        ],
    }


def test_verify_apple_subscription_active_is_ok(apple_key_path, ec_private_pem, monkeypatch):
    body = _apple_status_body(ec_private_pem, status=1)
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", "jws-from-client", False)

    assert result.ok is True
    assert result.transaction_id == "orig-1"
    assert result.expires_at == datetime(2100, 1, 1, 0, 0, tzinfo=timezone.utc)


def test_verify_apple_subscription_grace_period_is_ok(apple_key_path, ec_private_pem, monkeypatch):
    body = _apple_status_body(ec_private_pem, status=4)
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", "jws", False)
    assert result.ok is True


@pytest.mark.parametrize("status", [2, 3, 5])  # EXPIRED, BILLING_RETRY, REVOKED
def test_verify_apple_subscription_other_statuses_are_invalid(status, apple_key_path, ec_private_pem, monkeypatch):
    body = _apple_status_body(ec_private_pem, status=status)
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", "jws", False)
    assert result.ok is False and result.reason == "invalid"


def test_verify_apple_subscription_wrong_product_is_invalid(apple_key_path, ec_private_pem, monkeypatch):
    body = _apple_status_body(ec_private_pem, status=1, product="bt_pro_monthly")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    result = iap._verify_apple("subscription", "bt_max_monthly", "orig-1", "jws", False)
    assert result.ok is False and result.reason == "invalid"


def test_verify_apple_subscription_404_is_invalid(apple_key_path, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(404, {}))
    result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", "jws", False)
    assert result.ok is False and result.reason == "invalid"


def test_verify_apple_server_error_is_unavailable(apple_key_path, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(500, {}))
    result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", "jws", False)
    assert result.ok is False and result.reason == "unavailable"


def test_verify_apple_missing_key_is_unavailable(monkeypatch):
    monkeypatch.setattr(app_settings, "APPSTORE_IAP_KEY_PATH", None)
    result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", "jws", False)
    assert result.ok is False and result.reason == "unavailable"


@pytest.mark.parametrize("env_value,expected", [
    ("Sandbox", True), ("sandbox", True), ("SANDBOX", True),
    ("Production", False), ("production", False), (None, False),
])
def test_apple_sandbox_detection_is_case_insensitive(env_value, expected):
    info = {"environment": env_value} if env_value is not None else {}
    assert iap._is_apple_sandbox_transaction(info) is expected


def test_verify_apple_subscription_sandbox_environment_is_detected(apple_key_path, ec_private_pem, monkeypatch):
    body = _apple_status_body(ec_private_pem, status=1, environment="sandbox")  # 소문자로 와도
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", "jws", True)
    assert result.ok is True
    assert result.store_confirmed_test is True


# --------------------------------------------------------------------------- #
# 7) §26(2026-09-30, iOS 심사 제출 차단 긴급) — 환경 호스트 자동판정·폴백
#
# 실기기 사고: TestFlight/심사관 빌드는 클라가 is_sandbox=false 로 보내는데,
# 샌드박스 결제를 운영 호스트(api.storekit.apple.com)로 조회하면 401(출시 전)
# 또는 404(출시 후, 본문 4040010 TransactionIdNotFound)가 난다 — 인앱결제 심사가
# 실패해 거절이 확정되는 사고였다. 클라 JWS(purchase_token)의 environment 를
# "어느 호스트를 먼저 칠지" 힌트로만 쓰고, 틀리면 반대 호스트로 폴백한다.
# --------------------------------------------------------------------------- #
def _client_jws(ec_private_pem, *, environment="Production"):
    """purchase_token 자리에 넣을, 클라가 보냈다고 가정하는 StoreKit2 거래 JWS.
    힌트 판정만 검증하므로 environment 외 클레임은 필요 없다."""
    return jwt.encode({"environment": environment}, ec_private_pem, algorithm="ES256")


def _apple_dispatch_get(calls, *, prod=None, sandbox=None):
    """호스트별로 다른 응답을 주는 fake httpx.get — 실제로 어느 호스트를 먼저/나중에
    쳤는지 calls 리스트로 순서까지 확인할 수 있다."""
    def _get(url, **kw):
        calls.append(url)
        if url.startswith(iap._APPLE_SANDBOX_HOST):
            return sandbox
        return prod
    return _get


def test_apple_env_hint_sandbox_tries_sandbox_host_first(apple_key_path, ec_private_pem, monkeypatch):
    client_jws = _client_jws(ec_private_pem, environment="Sandbox")
    body = _apple_status_body(ec_private_pem, status=1, environment="Sandbox")
    calls: list = []
    monkeypatch.setattr(httpx, "get", _apple_dispatch_get(calls, sandbox=_FakeResponse(200, body)))

    result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", client_jws, False)

    assert result.ok is True
    assert len(calls) == 1
    assert calls[0].startswith(iap._APPLE_SANDBOX_HOST), "Sandbox 힌트인데 운영 호스트를 먼저 쳤다"


def test_apple_env_hint_production_with_401_falls_back_to_sandbox(apple_key_path, ec_private_pem, monkeypatch, caplog):
    """⭐⭐ 실기기 사고 그 자체 — 심사 제출 빌드(is_sandbox=false)가 실은 샌드박스
    결제라 운영 호스트가 401 을 주는 상황. 폴백으로 200 을 받아야 하고, 폴백
    로그 1줄에 purchase_token 이 찍히면 안 된다."""
    client_jws = _client_jws(ec_private_pem, environment="Production")
    body = _apple_status_body(ec_private_pem, status=1, environment="Sandbox")
    calls: list = []
    monkeypatch.setattr(httpx, "get", _apple_dispatch_get(
        calls, prod=_FakeResponse(401, {}), sandbox=_FakeResponse(200, body),
    ))

    with caplog.at_level("INFO", logger="core.iap"):
        result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", client_jws, False)

    assert result.ok is True
    assert len(calls) == 2
    assert calls[0].startswith(iap._APPLE_PRODUCTION_HOST)
    assert calls[1].startswith(iap._APPLE_SANDBOX_HOST)
    fallback_logs = [r.getMessage() for r in caplog.records if "환경 폴백" in r.getMessage()]
    assert len(fallback_logs) == 1
    assert "401" in fallback_logs[0]
    assert client_jws not in fallback_logs[0]


def test_apple_production_404_with_not_found_body_falls_back_to_sandbox(apple_key_path, ec_private_pem, monkeypatch):
    """출시 후엔 401 대신 404(본문 4040010 TransactionIdNotFound)가 온다 — 같은
    폴백이 적용돼야 한다."""
    body = _apple_status_body(ec_private_pem, status=1, environment="Sandbox")
    calls: list = []
    monkeypatch.setattr(httpx, "get", _apple_dispatch_get(
        calls,
        prod=_FakeResponse(404, {"errorCode": 4040010, "errorMessage": "Transaction id not found."}),
        sandbox=_FakeResponse(200, body),
    ))

    # 힌트 없음(purchase_token 파싱 실패) → 클라 is_sandbox=False 로 운영을 먼저 친다.
    result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", "not-a-jws", False)

    assert result.ok is True
    assert calls[0].startswith(iap._APPLE_PRODUCTION_HOST)
    assert calls[1].startswith(iap._APPLE_SANDBOX_HOST)


def test_apple_sandbox_401_falls_back_to_production_symmetrically(apple_key_path, ec_private_pem, monkeypatch):
    """반대 방향도 대칭이다 — 샌드박스를 먼저 쳤는데 401/404 면 운영으로 폴백."""
    client_jws = _client_jws(ec_private_pem, environment="Sandbox")
    body = _apple_status_body(ec_private_pem, status=1, environment="Production")
    calls: list = []
    monkeypatch.setattr(httpx, "get", _apple_dispatch_get(
        calls, prod=_FakeResponse(200, body), sandbox=_FakeResponse(401, {}),
    ))

    result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", client_jws, False)

    assert result.ok is True
    assert calls[0].startswith(iap._APPLE_SANDBOX_HOST)
    assert calls[1].startswith(iap._APPLE_PRODUCTION_HOST)
    assert result.store_confirmed_test is False


def test_apple_client_sandbox_lie_is_corrected_by_store_confirmed_test(apple_key_path, ec_private_pem, monkeypatch):
    """⭐⭐ §26(2026-09-30) — 클라가 is_sandbox=false 라고 보내도(심사 제출 빌드가
    그렇다) 실제로 샌드박스에서 200 을 받으면 store_confirmed_test=True 로 잡혀야
    한다(하류에서 is_sandbox = 클라 OR 스토어 로 합쳐 매출 집계에서 빠진다)."""
    body = _apple_status_body(ec_private_pem, status=1, environment="Sandbox")
    calls: list = []
    monkeypatch.setattr(httpx, "get", _apple_dispatch_get(
        calls, prod=_FakeResponse(401, {}), sandbox=_FakeResponse(200, body),
    ))

    result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", "not-a-jws", False)

    assert result.ok is True
    assert result.store_confirmed_test is True


def test_apple_hint_missing_or_unparseable_falls_back_to_client_flag_and_fallback_still_works(
    apple_key_path, ec_private_pem, monkeypatch,
):
    """힌트가 없거나(빈 문자열) JWS 파싱이 실패해도 기존 동작(클라 is_sandbox 값으로
    1순위를 고름)으로 시도하고, 그래도 틀리면 폴백이 정상 작동한다."""
    assert iap._apple_env_hint_unverified("garbage-not-a-jws") is None

    body = _apple_status_body(ec_private_pem, status=1, environment="Sandbox")
    calls: list = []
    monkeypatch.setattr(httpx, "get", _apple_dispatch_get(
        calls, prod=_FakeResponse(401, {}), sandbox=_FakeResponse(200, body),
    ))

    result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", "garbage-not-a-jws", False)

    assert result.ok is True
    assert calls[0].startswith(iap._APPLE_PRODUCTION_HOST), "힌트 없으면 클라 is_sandbox=False → 운영 먼저"
    assert calls[1].startswith(iap._APPLE_SANDBOX_HOST)


def test_apple_env_fallback_applies_to_character_kind_too(apple_key_path, ec_private_pem, monkeypatch):
    """묶음·캐릭터(일회성, Get Transaction Info)도 같은 폴백을 탄다."""
    jws = _apple_tx_jws(ec_private_pem, product="bt_character_bibi", original_tx="orig-2",
                         environment="Sandbox")
    calls: list = []
    monkeypatch.setattr(httpx, "get", _apple_dispatch_get(
        calls, prod=_FakeResponse(401, {}),
        sandbox=_FakeResponse(200, {"signedTransactionInfo": jws}),
    ))

    result = iap._verify_apple("character", "bt_character_bibi", "orig-2", "not-a-jws", False)

    assert result.ok is True
    assert calls[0].startswith(iap._APPLE_PRODUCTION_HOST)
    assert calls[1].startswith(iap._APPLE_SANDBOX_HOST)


def test_apple_host_strings_unchanged():
    """앱팀이 실측한 그 두 호스트 문자열 그대로인지 못박는다."""
    assert iap._APPLE_PRODUCTION_HOST == "https://api.storekit.apple.com"
    assert iap._APPLE_SANDBOX_HOST == "https://api.storekit-sandbox.apple.com"


def test_public_verify_entrypoint_also_gets_the_env_fallback(apple_key_path, ec_private_pem, monkeypatch):
    """⭐⭐ §26(2026-09-30) 요청 ②확인 — subscription_refresh_service(§24 자기치유·
    재검증)와 iap_service(verify_and_grant)는 둘 다 `_verify_apple` 을 직접 부르지
    않고 공용 진입점 `iap.verify()` 를 부른다(grep 으로 확인: 두 파일 다
    `iap.verify(` 한 줄뿐, `_verify_apple` 직접 호출 0건) — 그래서 이 픽스는 한
    곳만 고쳐도 두 경로 모두에 자동 적용된다. 이 시험은 그 공용 진입점을 통해서도
    폴백이 실제로 동작하는지 증명한다(내부 구현 우연히 안 타는 경로가 없는지)."""
    monkeypatch.setattr(app_settings, "IAP_VERIFY_ENABLED", True)
    body = _apple_status_body(ec_private_pem, status=1, environment="Sandbox")
    calls: list = []
    monkeypatch.setattr(httpx, "get", _apple_dispatch_get(
        calls, prod=_FakeResponse(401, {}), sandbox=_FakeResponse(200, body),
    ))

    result = iap.verify(
        platform="ios", kind="subscription", product_id="bt_pro_monthly",
        transaction_id="orig-1", purchase_token="not-a-jws", is_sandbox=False,
    )

    assert result.ok is True
    assert len(calls) == 2, "공용 진입점을 통해서도 운영→샌드박스 폴백이 일어나야 한다"


# --------------------------------------------------------------------------- #
# 8) §26(2026-09-30, 3차) — 애플 샌드박스 일시 5xx 재시도
#
# 실사고: 환경 폴백(2차)까지는 정상 작동해 샌드박스로 갔는데, 그 샌드박스
# 호스트 자체가 일시적으로 500 을 줬다. bt-back 이 같은 거래ID·같은 URL·4가지
# JWT 구성으로 직접 호출해 전부 200 을 받아 키·설정·JWT 구성은 배제했다 — 남은
# 설명은 "그 순간 애플이 일시적으로 아팠다"뿐이다. 5xx(401/404 와는 다른 축)에
# 만 짧은 재시도(2회, 0.5s→1.5s, 합 2.0s)를 건다.
# --------------------------------------------------------------------------- #
def _sequence_get(responses):
    """호출마다 정해진 응답을 순서대로 돌려주는 fake httpx.get(목록 소진되면
    마지막 값을 반복). 호출된 url 목록도 같이 돌려준다."""
    calls: list = []

    def _get(url, **kw):
        calls.append(url)
        idx = min(len(calls) - 1, len(responses) - 1)
        return responses[idx]

    return _get, calls


def _record_sleep(monkeypatch):
    delays: list = []
    monkeypatch.setattr(iap.time, "sleep", lambda s: delays.append(s))
    return delays


def test_apple_5xx_then_200_retries_and_succeeds(apple_key_path, ec_private_pem, monkeypatch):
    """⭐⭐ 실사고 그 자체 — 첫 호출 500, 재시도에서 200. verify 는 최종 성공해야
    한다(지금은 첫 500 에서 바로 unavailable 로 끝난다)."""
    body = _apple_status_body(ec_private_pem, status=1)
    get_fn, calls = _sequence_get([_FakeResponse(500, {}), _FakeResponse(200, body)])
    monkeypatch.setattr(httpx, "get", get_fn)
    delays = _record_sleep(monkeypatch)

    result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", "not-a-jws", False)

    assert result.ok is True
    assert len(calls) == 2
    assert delays == [0.5]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_apple_5xx_variants_all_retry(status, apple_key_path, ec_private_pem, monkeypatch):
    body = _apple_status_body(ec_private_pem, status=1)
    get_fn, calls = _sequence_get([_FakeResponse(status, {}), _FakeResponse(200, body)])
    monkeypatch.setattr(httpx, "get", get_fn)
    _record_sleep(monkeypatch)

    result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", "not-a-jws", False)

    assert result.ok is True
    assert len(calls) == 2


def test_apple_5xx_exhausted_returns_unavailable_with_retry_log_no_token(
    apple_key_path, monkeypatch, caplog,
):
    """계속 500 이면 재시도를 소진하고 unavailable(503) — 재시도 로그가 남고
    purchase_token 은 어디에도 안 찍힌다."""
    secret_token = "super-secret-client-jws"
    get_fn, calls = _sequence_get([_FakeResponse(500, {})])  # 항상 500
    monkeypatch.setattr(httpx, "get", get_fn)
    delays = _record_sleep(monkeypatch)

    with caplog.at_level("INFO", logger="core.iap"):
        result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", secret_token, False)

    assert result.ok is False and result.reason == "unavailable"
    assert len(calls) == 3, "초회 + 재시도 2회 = 3회 호출이어야 한다"
    retry_logs = [r.getMessage() for r in caplog.records if "5xx 재시도" in r.getMessage()]
    assert len(retry_logs) == 2
    assert delays == [0.5, 1.5]
    assert sum(delays) <= 3.0, "재시도 총 추가 지연이 상한 3초를 넘었다"
    for msg in retry_logs:
        assert secret_token not in msg


def test_apple_401_does_not_trigger_5xx_retry_only_env_fallback(
    apple_key_path, ec_private_pem, monkeypatch,
):
    """401/404(환경 틀림)는 5xx 재시도 축과 다르다 — 재시도 없이 바로 반대
    호스트로 폴백해야 한다(time.sleep 이 호출되면 안 된다)."""
    body = _apple_status_body(ec_private_pem, status=1, environment="Sandbox")
    calls: list = []
    monkeypatch.setattr(httpx, "get", _apple_dispatch_get(
        calls, prod=_FakeResponse(401, {}), sandbox=_FakeResponse(200, body),
    ))
    delays = _record_sleep(monkeypatch)

    result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", "not-a-jws", False)

    assert result.ok is True
    assert delays == [], "401 은 5xx 재시도를 타면 안 된다"


def test_apple_404_still_falls_back_after_5xx_retry_change(apple_key_path, monkeypatch):
    """기존 404→invalid 동작이 5xx 재시도 추가로 깨지지 않았는지 회귀."""
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(404, {}))
    result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", "jws", False)
    assert result.ok is False and result.reason == "invalid"


def test_verify_google_5xx_then_200_retries_and_succeeds(google_sa_key_path, monkeypatch):
    """⭐⭐ bt-back 요청 — 구글도 같은 구멍(raise_for_status 앞에 재시도 없음)이라
    같은 재시도를 적용했다(5xx 는 드물지만 안 넣을 이유가 없다)."""
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    get_fn, calls = _sequence_get([_FakeResponse(500, {}), _FakeResponse(200, _sub_body())])
    monkeypatch.setattr(httpx, "get", get_fn)
    delays = _record_sleep(monkeypatch)

    result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)

    assert result.ok is True
    assert delays == [0.5]


def test_verify_google_5xx_exhausted_returns_unavailable(google_sa_key_path, monkeypatch, caplog):
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    get_fn, calls = _sequence_get([_FakeResponse(500, {})])
    monkeypatch.setattr(httpx, "get", get_fn)
    delays = _record_sleep(monkeypatch)

    with caplog.at_level("INFO", logger="core.iap"):
        result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)

    assert result.ok is False and result.reason == "unavailable"
    assert len(calls) == 3
    assert delays == [0.5, 1.5]


def test_verify_google_400_does_not_trigger_5xx_retry(google_sa_key_path, monkeypatch):
    """400(구글의 무효 판정)은 5xx 축과 무관 — 재시도 없이 바로 invalid."""
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(400, {}))
    delays = _record_sleep(monkeypatch)

    result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)

    assert result.ok is False and result.reason == "invalid"
    assert delays == []


# --------------------------------------------------------------------------- #
# 7) 애플 캐릭터(일회성) 검증
# --------------------------------------------------------------------------- #
def test_verify_apple_character_matching_product_is_ok(apple_key_path, ec_private_pem, monkeypatch):
    jws = _apple_tx_jws(ec_private_pem, product="bt_character_bibi", original_tx="orig-2")
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: _FakeResponse(200, {"signedTransactionInfo": jws}),
    )

    result = iap._verify_apple("character", "bt_character_bibi", "orig-2", "jws", False)
    assert result.ok is True
    assert result.transaction_id == "orig-2"


def test_verify_apple_character_wrong_product_is_invalid(apple_key_path, ec_private_pem, monkeypatch):
    jws = _apple_tx_jws(ec_private_pem, product="bt_character_bibi")
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: _FakeResponse(200, {"signedTransactionInfo": jws}),
    )

    result = iap._verify_apple("character", "bt_character_popo", "orig-2", "jws", False)
    assert result.ok is False and result.reason == "invalid"


def test_verify_apple_character_404_is_invalid(apple_key_path, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(404, {}))
    result = iap._verify_apple("character", "bt_character_bibi", "orig-2", "jws", False)
    assert result.ok is False and result.reason == "invalid"


# --------------------------------------------------------------------------- #
# 8) acknowledge — 애플은 항상 no-op, 애초에 HTTP 를 안 부른다
# --------------------------------------------------------------------------- #
def test_acknowledge_ios_never_calls_http(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("애플 경로에서 HTTP 호출이 있으면 안 된다")

    monkeypatch.setattr(httpx, "post", _boom)
    monkeypatch.setattr(httpx, "get", _boom)
    assert iap.acknowledge("ios", "subscription", "tok", "p") is True


# --------------------------------------------------------------------------- #
# 9) verify() 배선 — sandbox 불일치 로그(bt-back 조건①)
# --------------------------------------------------------------------------- #
def test_verify_logs_warning_when_client_and_store_sandbox_flags_disagree(
    google_sa_key_path, monkeypatch, caplog
):
    monkeypatch.setattr(app_settings, "IAP_VERIFY_ENABLED", True)
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    body = _sub_body(test_purchase=True)  # 스토어=진짜 테스트
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    with caplog.at_level("WARNING", logger="core.iap"):
        result = iap.verify(
            platform="android", kind="subscription", product_id="bt_pro_monthly",
            transaction_id="tx-1", purchase_token="ptok", is_sandbox=False,  # 클라=거짓
        )

    assert result.ok is True and result.store_confirmed_test is True
    assert any("sandbox 불일치" in r.message for r in caplog.records)


def test_verify_no_warning_when_flags_agree(google_sa_key_path, monkeypatch, caplog):
    monkeypatch.setattr(app_settings, "IAP_VERIFY_ENABLED", True)
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    body = _sub_body(test_purchase=False)
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    with caplog.at_level("WARNING", logger="core.iap"):
        iap.verify(
            platform="android", kind="subscription", product_id="bt_pro_monthly",
            transaction_id="tx-1", purchase_token="ptok", is_sandbox=False,
        )

    assert not any("sandbox 불일치" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# 10) §23(2026-09-28) — invalid 사유를 로그에 남긴다(HTTP 상태·subscriptionState·
#     받은 productId·basePlanId, Google 은 카탈로그 존재 여부까지)
# --------------------------------------------------------------------------- #
def _dispatch_google_get(*, verify_status, verify_body=None, catalog_status=200, catalog_body=None):
    """토큰/거래 조회 호출과 카탈로그 조회 호출을 URL 로 구분해 서로 다른 응답을 준다."""
    def _get(url, **kw):
        if "subscriptionsv2/tokens" in url or "/purchases/products/" in url:
            return _FakeResponse(verify_status, verify_body or {})
        return _FakeResponse(catalog_status, catalog_body or {})
    return _get


def test_verify_google_subscription_400_logs_status_tx_and_catalog_membership(
    google_sa_key_path, monkeypatch, caplog,
):
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    monkeypatch.setattr(httpx, "get", _dispatch_google_get(
        verify_status=400,
        catalog_body={"subscriptions": [{"productId": "bt_pro_monthly"}]},
    ))

    with caplog.at_level("WARNING", logger="core.iap"):
        result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "super-secret-ptok", False)

    assert result.ok is False and result.reason == "invalid"
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "400" in msg
    assert "bt_pro_monthly" in msg
    assert "tx-1" in msg
    assert "카탈로그존재=True" in msg
    assert "super-secret-ptok" not in msg  # purchase_token 은 절대 안 찍는다


def test_verify_google_subscription_404_logs_product_not_in_catalog(
    google_sa_key_path, monkeypatch, caplog,
):
    """상품 자체가 스토어에 없는 경우 — "앱이 오타 상품을 보냈다" 를 구분할 수 있다."""
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    monkeypatch.setattr(httpx, "get", _dispatch_google_get(
        verify_status=404, catalog_body={"subscriptions": []},
    ))

    with caplog.at_level("WARNING", logger="core.iap"):
        result = iap._verify_google("subscription", "bt_typo_monthly", "tx-1", "ptok", False)

    assert result.ok is False and result.reason == "invalid"
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "카탈로그존재=False" in msg


def test_verify_google_subscription_400_catalog_lookup_failure_logs_unknown(
    google_sa_key_path, monkeypatch, caplog,
):
    """카탈로그 조회 자체가 실패해도(권한 등) 원래 422 응답은 그대로고, 로그만 '모름'."""
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    monkeypatch.setattr(httpx, "get", _dispatch_google_get(verify_status=400, catalog_status=500))

    with caplog.at_level("WARNING", logger="core.iap"):
        result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)

    assert result.ok is False and result.reason == "invalid"  # 카탈로그 실패가 원 응답을 안 막는다
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "카탈로그존재=None" in msg


def test_verify_google_subscription_state_mismatch_logs_state_and_product_lists(
    google_sa_key_path, monkeypatch, caplog,
):
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    body = _sub_body(state="SUBSCRIPTION_STATE_EXPIRED", product="bt_pro_monthly")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    with caplog.at_level("WARNING", logger="core.iap"):
        result = iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)

    assert result.ok is False
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "SUBSCRIPTION_STATE_EXPIRED" in msg
    assert "bt_pro_monthly" in msg


def test_verify_google_subscription_wrong_product_logs_requested_vs_seen(
    google_sa_key_path, monkeypatch, caplog,
):
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    body = _sub_body(product="bt_pro_monthly")  # 응답엔 pro 만 있음
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    with caplog.at_level("WARNING", logger="core.iap"):
        result = iap._verify_google("subscription", "bt_max_monthly", "tx-1", "ptok", False)  # 요청은 max

    assert result.ok is False
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "bt_max_monthly" in msg  # 요청 productId
    assert "bt_pro_monthly" in msg  # 응답에 실제로 있던 productId(비교용)


def test_verify_google_subscription_basePlanId_logged_when_present(
    google_sa_key_path, monkeypatch, caplog,
):
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    body = _sub_body(
        state="SUBSCRIPTION_STATE_EXPIRED",
        extra_line_item={"offerDetails": {"basePlanId": "monthly-base"}},
    )
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    with caplog.at_level("WARNING", logger="core.iap"):
        iap._verify_google("subscription", "bt_pro_monthly", "tx-1", "ptok", False)

    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "monthly-base" in msg


def test_verify_google_character_purchase_state_logged(google_sa_key_path, monkeypatch, caplog):
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, {"purchaseState": 1}))

    with caplog.at_level("WARNING", logger="core.iap"):
        result = iap._verify_google("character", "bt_character_bibi", "tx-1", "ptok", False)

    assert result.ok is False
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "purchaseState=1" in msg
    assert "bt_character_bibi" in msg


def test_verify_google_character_404_logs_catalog_membership(google_sa_key_path, monkeypatch, caplog):
    monkeypatch.setattr(httpx, "post", _fake_google_token_post())
    monkeypatch.setattr(httpx, "get", _dispatch_google_get(
        verify_status=404,
        catalog_body={"oneTimeProducts": [{"productId": "bt_character_bibi"}]},
    ))

    with caplog.at_level("WARNING", logger="core.iap"):
        result = iap._verify_google("character", "bt_character_bibi", "tx-1", "ptok", False)

    assert result.ok is False
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "카탈로그존재=True" in msg


def test_verify_apple_subscription_404_logs_status_and_tx(apple_key_path, monkeypatch, caplog):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(404, {}))

    with caplog.at_level("WARNING", logger="core.iap"):
        result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", "super-secret-jws", False)

    assert result.ok is False
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "404" in msg
    assert "orig-1" in msg
    assert "super-secret-jws" not in msg


def test_verify_apple_subscription_wrong_product_logs_seen_ids(apple_key_path, ec_private_pem, monkeypatch, caplog):
    body = _apple_status_body(ec_private_pem, status=1, product="bt_pro_monthly")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    with caplog.at_level("WARNING", logger="core.iap"):
        result = iap._verify_apple("subscription", "bt_max_monthly", "orig-1", "jws", False)

    assert result.ok is False
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "bt_max_monthly" in msg
    assert "bt_pro_monthly" in msg


def test_verify_apple_subscription_bad_status_logs_status_and_group(apple_key_path, ec_private_pem, monkeypatch, caplog):
    body = _apple_status_body(ec_private_pem, status=2, product="bt_pro_monthly")
    body["data"][0]["lastTransactions"][0]["signedTransactionInfo"] = jwt.encode(
        {
            "productId": "bt_pro_monthly", "originalTransactionId": "orig-1",
            "expiresDate": 4102444800000, "environment": "Production",
            "subscriptionGroupIdentifier": "group-42",
        },
        ec_private_pem, algorithm="ES256",
    )
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, body))

    with caplog.at_level("WARNING", logger="core.iap"):
        result = iap._verify_apple("subscription", "bt_pro_monthly", "orig-1", "jws", False)

    assert result.ok is False
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "status=2" in msg
    assert "group-42" in msg


def test_verify_apple_character_404_logs_status(apple_key_path, monkeypatch, caplog):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(404, {}))

    with caplog.at_level("WARNING", logger="core.iap"):
        result = iap._verify_apple("character", "bt_character_bibi", "orig-2", "jws", False)

    assert result.ok is False
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "404" in msg
    assert "bt_character_bibi" in msg


def test_verify_apple_character_wrong_product_logs_both_ids(apple_key_path, ec_private_pem, monkeypatch, caplog):
    jws = _apple_tx_jws(ec_private_pem, product="bt_character_bibi")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeResponse(200, {"signedTransactionInfo": jws}))

    with caplog.at_level("WARNING", logger="core.iap"):
        result = iap._verify_apple("character", "bt_character_popo", "orig-2", "jws", False)

    assert result.ok is False
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "bt_character_popo" in msg  # 요청
    assert "bt_character_bibi" in msg  # 실제 응답
