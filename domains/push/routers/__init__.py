"""push 도메인 라우터 집합 — 기기 토큰(공개) + 내부 디스패치."""

from fastapi import APIRouter

from domains.push.routers.device import router as device_router
from domains.push.routers.internal import router as internal_router

router = APIRouter()
router.include_router(device_router)
router.include_router(internal_router)

__all__ = ["router"]
