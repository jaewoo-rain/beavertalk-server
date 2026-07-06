"""internal 라우터 — 예약전화 디스패처 트리거(외부 크론 전용, 스키마 비노출).

인증은 회원 JWT 가 아니라 공유 시크릿 헤더(X-Internal-Secret)로 한다. 시크릿
미설정이면 항상 403(실수로 열려 있지 않도록). 비교는 타이밍 세이프.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException, status

from core.config import settings
from core.deps import DbSession
from domains.push.service.dispatch_service import DispatchService

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/dispatch-calls", include_in_schema=False)
def dispatch_calls(
    db: DbSession, x_internal_secret: str = Header(default="")
) -> dict:
    """예약전화 디스패치 1회 실행(크론이 1분마다 호출). {"dispatched": n} 반환."""
    if not settings.INTERNAL_DISPATCH_SECRET:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if not hmac.compare_digest(x_internal_secret, settings.INTERNAL_DISPATCH_SECRET):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    return {"dispatched": DispatchService(db).run()}
