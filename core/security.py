"""보안 유틸 — 비밀번호 해싱 + JWT 발급/검증 (순수 함수).

Spring Security 의 PasswordEncoder + JwtTokenProvider 에 해당.
여기는 도메인/DB 를 모른다(순수). 사용자 조회까지 묶는 인증 의존성은 core/deps.py 에 있다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from fastapi.security import OAuth2PasswordBearer

from core.config import settings

# 로그인 엔드포인트(토큰 발급처). Swagger UI 의 Authorize 버튼이 이 경로를 사용.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

# bcrypt 는 72바이트까지만 사용 → 초과분은 잘라서 일관 처리
_BCRYPT_MAX_BYTES = 72


def hash_password(raw: str) -> str:
    """평문 비밀번호 → bcrypt 해시 문자열."""
    pw = raw.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(raw: str, hashed: str) -> bool:
    """평문이 해시와 일치하는지 검사."""
    pw = raw.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    try:
        return bcrypt.checkpw(pw, hashed.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(subject: str | int, expires_minutes: int | None = None) -> str:
    """subject(보통 member_id)를 담은 JWT 액세스 토큰 발급."""
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload: dict[str, Any] = {"sub": str(subject), "exp": expire, "purpose": "access"}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """JWT 검증 + 디코드. 실패 시 jwt.PyJWTError 계열 예외를 던진다."""
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
