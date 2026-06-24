"""FastAPI 앱 진입점.

- create_app(settings) 팩토리로 앱을 만든다(테스트에서 설정 주입 가능).
- lifespan 에서 엔진/세션 팩토리를 만들어 app.state 에 보관(전역 엔진 없음).
- /api/v1 하위에 도메인 라우터 등록.
- HTTPException 을 표준 에러 바디({"detail": {"code","message"}})로 변환.
- 헬스체크 엔드포인트.

실행: uvicorn main:app --reload
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from core.config import Settings
from core.config import settings as default_settings
from db.engine import build_engine
from db.session import build_session_factory
from domains.account.routers import router as account_router
from domains.alarm.routers import router as alarm_router
from domains.commerce.routers import router as commerce_router
from domains.learning.routers import router as learning_router

API_PREFIX = "/api/v1"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """앱 수명 동안 공유 자원(엔진/세션 팩토리)을 준비하고 종료 시 정리한다."""
    settings: Settings = app.state.settings
    engine = build_engine(settings)
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)
    try:
        yield
    finally:
        engine.dispose()  # 종료 시 커넥션 정리


async def http_exception_handler(
    request: Request, exc: FastAPIHTTPException
) -> JSONResponse:
    """HTTPException → 표준 에러 바디로 통일.

    raise HTTPException(409, "이미 가입된 이메일입니다.") 처럼 문자열만 줘도
    {"detail": {"code": "HTTP_409", "message": "..."}} 로 감싼다.
    이미 dict(detail) 를 준 경우는 그대로 통과.
    """
    detail = exc.detail
    if isinstance(detail, dict):
        body = {"detail": detail}
    else:
        body = {"detail": {"code": f"HTTP_{exc.status_code}", "message": str(detail)}}
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


def create_app(settings: Settings | None = None) -> FastAPI:
    """FastAPI 앱 팩토리. settings 미지정 시 .env 로 로드된 기본 설정 사용."""
    settings = settings or default_settings

    app = FastAPI(
        title="BeaverTalk API",
        version="0.1.0",
        docs_url="/docs",
        lifespan=lifespan,
    )
    app.state.settings = settings  # lifespan 이 이걸 읽는다(이중 해석 방지)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_exception_handler(FastAPIHTTPException, http_exception_handler)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        """헬스체크 (DB 연결은 확인하지 않음)."""
        return {"status": "ok", "env": settings.ENV}

    # ── 도메인 라우터 등록 ──
    app.include_router(account_router, prefix=API_PREFIX)
    app.include_router(commerce_router, prefix=API_PREFIX)
    app.include_router(alarm_router, prefix=API_PREFIX)
    app.include_router(learning_router, prefix=API_PREFIX)

    # ── (dev 전용) 단일 HTML API 테스트 콘솔 ──
    # 운영(prod)에는 노출하지 않는다. 같은 오리진으로 서빙하므로 CORS 불필요.
    if settings.ENV != "prod":

        @app.get("/__console", include_in_schema=False)
        def api_console() -> FileResponse:
            """브라우저로 API를 손테스트하는 단일 HTML 콘솔."""
            return FileResponse(
                Path(__file__).parent / "scripts" / "console.html",
                media_type="text/html",
            )

    return app


app = create_app()  # uvicorn main:app 호환
