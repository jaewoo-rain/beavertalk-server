"""internal 라우터 — 구독 갱신 스케줄 스윕 트리거(외부 크론 전용, 스키마 비노출).

인증은 회원 JWT 가 아니라 공유 시크릿 헤더(X-Internal-Secret)로 한다 — 예약전화
디스패치(domains/push/routers/internal.py)와 같은 패턴·같은 시크릿을 재사용한다
(§24 계획서 3단계 — "기존 /internal/dispatch-calls 의 인증·스케줄러 패턴을
그대로 재사용한다").
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException, status

from core.config import settings
from core.deps import DbSession
from domains.commerce.service.subscription_refresh_service import SubscriptionRefreshService

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/refresh-subscriptions", include_in_schema=False)
def refresh_subscriptions(
    db: DbSession, x_internal_secret: str = Header(default="")
) -> dict:
    """만료된 store 구독 스윕 1회 실행(크론 — 앱을 안 열어도 갱신/해지가 반영된다).

    {"refreshed": n} — 실제로 스토어를 불러 재조회를 시도한 건수(가드에 걸려
    건너뛴 건 제외, source='manual' 은 애초에 대상이 아니다).
    """
    if not settings.INTERNAL_DISPATCH_SECRET:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if not hmac.compare_digest(x_internal_secret, settings.INTERNAL_DISPATCH_SECRET):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    return {"refreshed": SubscriptionRefreshService(db).sweep()}
