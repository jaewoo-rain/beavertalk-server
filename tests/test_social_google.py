"""core.social 의 Google ID 토큰 검증 단위 테스트.

실제 Google JWKS 를 치지 않도록 `_google_signing_key` 를 자체 RSA 공개키로
monkeypatch 한다. 즉 "우리가 만든 키로 서명한 토큰" 을 Google 토큰처럼 검증시켜,
정상/위조/aud불일치/만료/iss불일치/이메일미검증 케이스를 확인한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import core.social as social
from core.config import settings
from core.social import SocialAuthError, verify_social_token

_CLIENT_ID = "test-client-id.apps.googleusercontent.com"


@pytest.fixture(scope="module")
def keys() -> tuple[str, str]:
    """RSA 키쌍 → (private_pem, public_pem)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture(autouse=True)
def _wire(monkeypatch, keys):
    """GOOGLE_CLIENT_ID 설정 + JWKS 키 조회를 자체 공개키로 대체."""
    _, public_pem = keys
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", _CLIENT_ID)
    monkeypatch.setattr(social, "_google_signing_key", lambda token: public_pem)


def _make_token(
    private_pem: str,
    *,
    sub: str = "google-sub-123",
    aud: str = _CLIENT_ID,
    iss: str = "https://accounts.google.com",
    email: str | None = "user@gmail.com",
    email_verified: object = True,
    exp_delta: timedelta = timedelta(hours=1),
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict = {
        "sub": sub,
        "aud": aud,
        "iss": iss,
        "iat": now,
        "exp": now + exp_delta,
    }
    if email is not None:
        payload["email"] = email
        payload["email_verified"] = email_verified
    return jwt.encode(payload, private_pem, algorithm="RS256")


def test_valid_token(keys):
    private_pem, _ = keys
    identity = verify_social_token("google", _make_token(private_pem))
    assert identity.login_method == "google"
    assert identity.unique_value == "google-sub-123"
    assert identity.email == "user@gmail.com"


def test_case_insensitive_provider(keys):
    private_pem, _ = keys
    identity = verify_social_token("Google", _make_token(private_pem))
    assert identity.unique_value == "google-sub-123"


def test_unverified_email_is_dropped(keys):
    private_pem, _ = keys
    token = _make_token(private_pem, email_verified=False)
    identity = verify_social_token("google", token)
    assert identity.email is None  # 미검증 이메일은 신뢰하지 않음


def test_wrong_audience_rejected(keys):
    private_pem, _ = keys
    token = _make_token(private_pem, aud="someone-elses-client-id")
    with pytest.raises(SocialAuthError):
        verify_social_token("google", token)


def test_expired_token_rejected(keys):
    private_pem, _ = keys
    token = _make_token(private_pem, exp_delta=timedelta(hours=-1))
    with pytest.raises(SocialAuthError):
        verify_social_token("google", token)


def test_bad_issuer_rejected(keys):
    private_pem, _ = keys
    token = _make_token(private_pem, iss="https://evil.example.com")
    with pytest.raises(SocialAuthError):
        verify_social_token("google", token)


def test_tampered_signature_rejected(keys):
    private_pem, _ = keys
    token = _make_token(private_pem)
    tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    with pytest.raises(SocialAuthError):
        verify_social_token("google", tampered)


def test_token_signed_by_other_key_rejected(keys):
    # 다른(공격자) 키로 서명 → 우리 공개키로 검증 실패해야 함.
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attacker_pem = attacker.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    token = _make_token(attacker_pem)
    with pytest.raises(SocialAuthError):
        verify_social_token("google", token)


def test_missing_client_id_is_server_error(keys, monkeypatch):
    private_pem, _ = keys
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", None)
    # 설정 누락은 클라이언트 잘못이 아니므로 RuntimeError(→500)로 구분.
    with pytest.raises(RuntimeError):
        verify_social_token("google", _make_token(private_pem))
