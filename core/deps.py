"""FastAPI 의존성 배선.

- get_db            : 요청 단위 세션 (db/session.py 재노출)
- get_current_member: JWT → 현재 회원 (Spring SecurityContext 의 인증 주체)
- PageParams        : 공통 페이지네이션 쿼리 파라미터

이 모듈은 '배선' 계층이라 core 이면서도 domains 를 import 한다(인증 주체가 Member 라서).
순수 암호화/토큰은 core/security.py, DB 접근은 repository 가 담당.
"""

from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import Depends, HTTPException, Query, Request, status
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


def get_genai_client(request: Request) -> Any | None:
    """lifespan 이 만든 공유 genai 클라이언트(app.state.genai_client)를 주입한다.

    Vertex 미구성/생성 실패 시 None — 핸들러가 None 을 503 으로 매핑한다.
    """
    return getattr(request.app.state, "genai_client", None)


ADMIN_ROLE = "admin"


def get_current_admin(
    member: Annotated[Member, Depends(get_current_member)],
) -> Member:
    """관리자 전용 — member.role != "admin" 이면 403.

    /__dev/* 운영 도구(할인 이벤트·레벨 초기화·롤 관리)가 이걸 쓴다. 그 도구들은 배포
    환경 판정(ENV != "prod")으로만 가려져 있었는데, 실서비스조차 ENV="test" 라 사실상
    **로그인한 아무 회원에게나** 열려 있었다.

    권한을 JWT 가 아니라 DB 에서 읽는다 — 권한 회수가 즉시 반영되고(JWT 는 만료까지
    옛 권한이 산다), get_current_member 가 이미 member 행을 읽으므로 비용도 0이다.
    """
    if getattr(member, "role", None) != ADMIN_ROLE:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": "ADMIN_ONLY", "message": "관리자 전용 기능입니다."},
        )
    return member


# 라우터에서 `member: CurrentMember` 로 간결하게 주입
CurrentMember = Annotated[Member, Depends(get_current_member)]
CurrentAdmin = Annotated[Member, Depends(get_current_admin)]
DbSession = Annotated[Session, Depends(get_db)]
GenaiClient = Annotated[Any, Depends(get_genai_client)]


class PageParams:
    """공통 페이지네이션 쿼리. `params: PageParams = Depends()` 로 사용."""

    def __init__(
        self,
        limit: int = Query(20, ge=1, le=100, description="페이지 크기"),
        offset: int = Query(0, ge=0, description="건너뛸 개수"),
    ) -> None:
        self.limit = limit
        self.offset = offset
