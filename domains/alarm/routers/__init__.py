"""alarm 도메인 라우터 집합."""

from fastapi import APIRouter

from domains.alarm.routers.alarm import router as alarm_router

router = APIRouter()
router.include_router(alarm_router)

__all__ = ["router"]
