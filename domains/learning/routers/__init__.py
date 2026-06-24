"""learning 도메인 라우터 집합."""

from fastapi import APIRouter

from domains.learning.routers.call import router as call_router
from domains.learning.routers.sentence import router as sentence_router

router = APIRouter()
router.include_router(call_router)
router.include_router(sentence_router)

__all__ = ["router"]
