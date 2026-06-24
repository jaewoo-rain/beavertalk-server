"""FastAPI 의존성 배선.

- get_db            : 요청 단위 세션 (db/session.py 재노출)
- get_current_member: JWT → 현재 회원 (Spring SecurityContext 의 인증 주체)
- PageParams        : 공통 페이지네이션 쿼리 파라미터

이 모듈은 '배선' 계층이라 core 이면서도 domains 를 import 한다(인증 주체가 Member 라서).
순수 암호화/토큰은 core/security.py, DB 접근은 repository 가 담당.
"""

from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from core.security import decode_token, oauth2_scheme
from db.session import get_db
from domains.account.models.member import Member
from domains.account.repository.member_repository import MemberRepository

_CREDENTIALS_EXC = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="인증 정보가 유효하지 않습니다.",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_member(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> Member:
    """Authorization: Bearer <jwt> → Member. 실패 시 401."""
    try:
        payload = decode_token(token)
        # access 토큰만 인증에 허용(비밀번호 재설정 토큰 등 다른 용도 토큰 거부)
        if payload.get("purpose") != "access":
            raise _CREDENTIALS_EXC
        subject = payload.get("sub")
        if subject is None:
            raise _CREDENTIALS_EXC
        member_id = int(subject)
    except (jwt.PyJWTError, ValueError):
        raise _CREDENTIALS_EXC

    member = MemberRepository(db).get(member_id)
    if member is None:
        raise _CREDENTIALS_EXC
    return member


# 라우터에서 `member: CurrentMember` 로 간결하게 주입
CurrentMember = Annotated[Member, Depends(get_current_member)]
DbSession = Annotated[Session, Depends(get_db)]


class PageParams:
    """공통 페이지네이션 쿼리. `params: PageParams = Depends()` 로 사용."""

    def __init__(
        self,
        limit: int = Query(20, ge=1, le=100, description="페이지 크기"),
        offset: int = Query(0, ge=0, description="건너뛸 개수"),
    ) -> None:
        self.limit = limit
        self.offset = offset
