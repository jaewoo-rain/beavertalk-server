"""commerce 도메인 라우터 집합."""

from fastapi import APIRouter

from domains.commerce.routers.character import router as character_router
from domains.commerce.routers.payment import router as payment_router
from domains.commerce.routers.subscription import router as subscription_router

router = APIRouter()
router.include_router(character_router)
router.include_router(payment_router)
router.include_router(subscription_router)

__all__ = ["router"]
