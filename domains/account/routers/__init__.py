"""account 도메인 라우터 집합. (인증은 Supabase Auth — /auth 라우터 없음)"""

from fastapi import APIRouter

from domains.account.routers.member import router as member_router

router = APIRouter()
router.include_router(member_router)

__all__ = ["router"]
