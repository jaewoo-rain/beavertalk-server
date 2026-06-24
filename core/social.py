"""소셜 로그인 토큰 검증.

provider 별로 검증 방식이 다르다:
- **Google** : ID 토큰(JWT)을 Google JWKS 공개키로 서명 검증 + aud/iss/exp 확인 (구현됨)
- **Apple**  : Apple 공개키로 검증(+ 비공개 이메일 릴레이) — 미구현
- **Kakao**  : 액세스 토큰으로 https://kapi.kakao.com/v2/user/me 호출 — 미구현(추후)

검증 후 '신뢰할 수 있는' 고유값(sub)과 이메일을 반환한다. 프론트에서 받은 raw 토큰을
서명/발급자 검증 없이 그대로 믿으면 위조가 가능하므로, 반드시 provider 신뢰기반으로 검증한다.

검증 실패(위조·만료·aud 불일치 등)는 [SocialAuthError] 로 던지고, 서비스 계층이 401 로
변환한다. 서버 설정 누락(GOOGLE_CLIENT_ID 미설정 등)은 RuntimeError(→500)로 구분한다.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Optional

import jwt
from jwt import PyJWKClient

from core.config import settings


@dataclass
class SocialIdentity:
    login_method: str          # provider (google/apple/kakao ...)
    unique_value: str          # 검증된 고유 식별자(sub)
    email: Optional[str] = None


class SocialAuthError(Exception):
    """소셜 토큰 검증 실패(위조/만료/aud·iss 불일치 등). 서비스가 401 로 변환."""


# ── Google ─────────────────────────────────────────────────────────────────
_GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}

_jwk_client: PyJWKClient | None = None
_jwk_lock = threading.Lock()


def _google_jwk_client() -> PyJWKClient:
    """Google JWKS 클라이언트(공개키 캐시). 프로세스 단위 싱글톤."""
    global _jwk_client
    if _jwk_client is None:
        with _jwk_lock:
            if _jwk_client is None:
                _jwk_client = PyJWKClient(_GOOGLE_CERTS_URL, cache_keys=True)
    return _jwk_client


def _google_signing_key(token: str) -> Any:
    """토큰 헤더(kid)에 맞는 Google 공개키를 JWKS 에서 찾아 반환.

    (테스트는 이 함수를 monkeypatch 해 자체 공개키를 주입한다.)
    """
    return _google_jwk_client().get_signing_key_from_jwt(token).key


def _truthy(value: Any) -> bool:
    """Google 의 email_verified 는 bool 또는 "true" 문자열로 올 수 있다."""
    return value is True or str(value).lower() == "true"


def _verify_google(token: str) -> SocialIdentity:
    """Google ID 토큰 검증 → SocialIdentity. 실패 시 [SocialAuthError]."""
    auds = settings.google_client_ids
    if not auds:
        # 설정 누락은 클라이언트 잘못이 아니므로 500 으로 구분.
        raise RuntimeError("GOOGLE_CLIENT_ID 가 설정되지 않았습니다(.env).")

    try:
        key = _google_signing_key(token)
        payload = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=list(auds),
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
        )
    except jwt.PyJWTError as exc:  # 서명/만료/aud 불일치 등
        raise SocialAuthError(f"유효하지 않은 Google 토큰입니다: {exc}") from exc
    except SocialAuthError:
        raise
    except Exception as exc:  # noqa: BLE001  (JWKS 조회 실패 등)
        raise SocialAuthError("Google 토큰 검증에 실패했습니다.") from exc

    if payload.get("iss") not in _GOOGLE_ISSUERS:
        raise SocialAuthError("Google 발급자(iss)가 올바르지 않습니다.")

    sub = payload.get("sub")
    if not sub:
        raise SocialAuthError("Google 토큰에 sub 가 없습니다.")

    # 이메일은 검증된(email_verified) 경우에만 신뢰한다.
    email = payload.get("email") if _truthy(payload.get("email_verified")) else None

    return SocialIdentity(login_method="google", unique_value=str(sub), email=email)


# ── dispatch ─────────────────────────────────────────────────────────────────
def verify_social_token(login_method: str, token: str) -> SocialIdentity:
    """provider 토큰 → 검증된 신원.

    - google : 실제 검증(JWKS 서명 + aud/iss/exp).
    - 그 외(kakao/apple) : 아직 실검증 미구현. dev 에선 토큰을 그대로 고유값으로 취급하는
      스텁으로 통과시키고, prod 에선 차단한다(스텁이 운영에 노출되는 사고 방지).
    """
    method = (login_method or "").strip().lower()

    if method == "google":
        return _verify_google(token)

    # kakao / apple 등 — 실검증 교체 전까지 dev 한정 스텁.
    if settings.ENV == "prod":
        raise SocialAuthError(
            f"{login_method} 소셜 로그인 검증이 아직 구현되지 않았습니다."
        )
    return SocialIdentity(login_method=method, unique_value=token, email=None)
