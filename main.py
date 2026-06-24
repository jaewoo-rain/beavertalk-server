"""FastAPI 앱 진입점.

- /api/v1 하위에 도메인 라우터 등록 (라우터는 이후 단계에서 추가)
- HTTPException 을 표준 에러 바디({"detail": {"code","message"}})로 변환
- 헬스체크 엔드포인트

실행: uvicorn main:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import JSONResponse

from core.config import settings

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="BeaverTalk API",
    version="0.1.0",
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_PREFIX = "/api/v1"

@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """헬스체크 (DB 연결은 확인하지 않음)."""
    return {"status": "ok", "env": settings.ENV}


@app.exception_handler(FastAPIHTTPException)
async def http_exception_handler(request: Request, exc: FastAPIHTTPException) -> JSONResponse:
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


# ── 도메인 라우터 등록 ──
from domains.account.routers import router as account_router  # noqa: E402
from domains.alarm.routers import router as alarm_router  # noqa: E402
from domains.commerce.routers import router as commerce_router  # noqa: E402
from domains.learning.routers import router as learning_router  # noqa: E402

app.include_router(account_router, prefix=API_PREFIX)
app.include_router(commerce_router, prefix=API_PREFIX)
app.include_router(alarm_router, prefix=API_PREFIX)
app.include_router(learning_router, prefix=API_PREFIX)


# ── (dev 전용) 단일 HTML API 테스트 콘솔 ──
# 운영(prod)에는 노출하지 않는다. 같은 오리진으로 서빙하므로 CORS 불필요.
if settings.ENV != "prod":
    from pathlib import Path  # noqa: E402

    from fastapi.responses import FileResponse  # noqa: E402

    @app.get("/__console", include_in_schema=False)
    def api_console() -> FileResponse:
        """브라우저로 API를 손테스트하는 단일 HTML 콘솔."""
        return FileResponse(
            Path(__file__).parent / "scripts" / "console.html",
            media_type="text/html",
        )
