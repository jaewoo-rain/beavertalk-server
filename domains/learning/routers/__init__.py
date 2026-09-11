"""learning 도메인 라우터 집합."""

from fastapi import APIRouter

from domains.learning.realtime.ws_router import router as realtime_router
from domains.learning.routers.call import router as call_router
from domains.learning.routers.curriculum import router as curriculum_router
from domains.learning.routers.sentence import router as sentence_router
from domains.learning.routers.tts import router as tts_router

router = APIRouter()
router.include_router(call_router)
router.include_router(curriculum_router)  # GET /cur/me · GET /cur/lessons — 커리큘럼 2단계 조회
router.include_router(sentence_router)
router.include_router(tts_router)  # POST /tts/speech — 임의 문장 → MP3(저장 안 함)
router.include_router(realtime_router)  # WS /calls/stream + GET /calls/{id}/status

__all__ = ["router"]
