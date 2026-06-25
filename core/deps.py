"""FastAPI 의존성 배선.

- get_db            : 요청 단위 세션 (db/session.py 재노출)
- get_current_member: JWT → 현재 회원 (Spring SecurityContext 의 인증 주체)
- PageParams        : 공통 페이지네이션 쿼리 파라미터

이 모듈은 '배선' 계층이라 core 이면서도 domains 를 import 한다(인증 주체가 Member 라서).
순수 암호화/토큰은 core/security.py, DB 접근은 repository 가 담당.
"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from core.supabase_auth import verify_token
from db.session import get_db
from domains.account.models.member import Member
from domains.account.service.member_service import MemberService

_CREDENTIALS_EXC = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="인증 정보가 유효하지 않습니다.",
    headers={"WWW-Authenticate": "Bearer"},
)

# Authorization: Bearer <Supabase access token>
_bearer = HTTPBearer(auto_error=False)


def get_current_member(
    creds: Annotated[Optional[HTTPAuthorizationCredentials], Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> Member:
    """Supabase access token(Bearer) 검증 → member(없으면 자동 생성). 실패 시 401.

    인증 자체는 Supabase Auth 가 담당하고, 우리는 토큰을 검증해 auth uuid 로 member 를
    찾거나(find) 처음이면 만든다(provision).
    """
    if creds is None or not creds.credentials:
        raise _CREDENTIALS_EXC
    auth_user = verify_token(creds.credentials)
    if auth_user is None:
        raise _CREDENTIALS_EXC
    return MemberService(db).find_or_create_by_auth(auth_user.uid, auth_user.email)


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
