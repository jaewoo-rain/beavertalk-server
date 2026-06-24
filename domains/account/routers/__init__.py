"""account 도메인 라우터 집합."""

from fastapi import APIRouter

from domains.account.routers.auth import router as auth_router
from domains.account.routers.member import router as member_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(member_router)

__all__ = ["router"]
