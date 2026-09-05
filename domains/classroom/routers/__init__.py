from fastapi import APIRouter

from domains.classroom.routers.classroom import router as classroom_router
from domains.classroom.routers.enrollment import router as enrollment_router

router = APIRouter()
router.include_router(classroom_router)
router.include_router(enrollment_router)
