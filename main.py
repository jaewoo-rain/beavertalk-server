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
import logging
import pathlib
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from core.config import Settings
from core.config import settings as default_settings
from core.deps import CurrentMember, DbSession
from db.engine import build_engine
from db.session import build_session_factory
from domains.account.routers import router as account_router
from domains.alarm.routers import router as alarm_router
from domains.commerce.routers import router as commerce_router
from domains.learning.routers import router as learning_router

API_PREFIX = "/api/v1"

logger = logging.getLogger(__name__)


def _create_genai_client(settings: Settings) -> Any | None:
    """normalcall 용 genai.Client 를 생성한다(실패 시 None — 통화만 비활성, 앱은 정상).

    USE_VERTEX=True 면 서비스계정 키(설정 경로 → 프로젝트 루트 gcp_key.json 폴백)로
    Vertex 클라이언트를, 아니면 GEMINI_API_KEY 로 AI Studio 클라이언트를 만든다.
    google-genai 미설치·키 부재·인증 실패 등 어떤 사유로도 None 을 반환한다(graceful).
    """
    try:
        from google import genai

        if settings.USE_VERTEX:
            from google.oauth2 import service_account

            key_path = settings.GOOGLE_APPLICATION_CREDENTIALS
            if not key_path or not pathlib.Path(key_path).is_file():
                local = pathlib.Path(__file__).resolve().parent / "gcp_key.json"
                key_path = str(local) if local.is_file() else None
            if not key_path:
                logger.warning("normalcall: Vertex 키 없음 → genai 비활성(통화 불가).")
                return None
            creds = service_account.Credentials.from_service_account_file(
                key_path, scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            logger.info(
                "normalcall: Vertex genai 클라이언트 생성 project=%s location=%s model=%s",
                settings.GCP_PROJECT, settings.GCP_LOCATION, settings.GEMINI_LIVE_MODEL,
            )
            return genai.Client(
                vertexai=True,
                project=settings.GCP_PROJECT,
                location=settings.GCP_LOCATION,
                credentials=creds,
            )
        if not settings.GEMINI_API_KEY:
            logger.warning("normalcall: GEMINI_API_KEY 없음 → genai 비활성(통화 불가).")
            return None
        return genai.Client(api_key=settings.GEMINI_API_KEY)
    except Exception as exc:  # noqa: BLE001 - 미설치/인증/임의 예외 graceful
        logger.warning("normalcall: genai 클라이언트 생성 실패 → 비활성: %s", exc)
        return None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """앱 수명 동안 공유 자원(엔진/세션 팩토리/genai)을 준비하고 종료 시 정리한다."""
    settings: Settings = app.state.settings
    engine = build_engine(settings)
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)
    app.state.genai_client = _create_genai_client(settings)  # normalcall(없으면 None)
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

        @app.get("/__calldemo", include_in_schema=False)
        def call_demo() -> FileResponse:
            """통화→문장추출→복습 발음평가 전 과정 데모 HTML."""
            return FileResponse(
                Path(__file__).parent / "scripts" / "call_demo.html",
                media_type="text/html",
            )

        @app.post("/__dev/pron-eval", include_in_schema=False)
        async def dev_pron_eval(  # type: ignore[no-untyped-def]
            member: CurrentMember,
            db: DbSession,
            sentence_id: int = Form(...),
            audio: UploadFile = File(...),
        ):
            """[dev] 브라우저 녹음(WAV) → MP3 인코딩 → Supabase voice-recordings 업로드.

            저장은 MP3(어디서든 재생), 채점은 무손실 원본 WAV(임시파일)로 한다.
            ffmpeg 없으면 WAV 로 저장 폴백, 스토리지 비활성이면 key=None(채점만).
            """
            import os
            import tempfile
            import uuid

            from core import audio as audio_mod
            from core import storage
            from domains.learning.schemas.review import ReviewCreate
            from domains.learning.service.review_service import ReviewService

            data = await audio.read()  # 브라우저가 보낸 WAV
            # 저장용: MP3 인코딩(실패하면 WAV 그대로).
            mp3 = audio_mod.wav_to_mp3(data)
            payload, ext, ctype = (
                (mp3, "mp3", "audio/mpeg") if mp3 else (data, "wav", "audio/wav")
            )
            key = f"reviews/{member.member_id}/{sentence_id}/{uuid.uuid4().hex}.{ext}"
            stored = storage.upload(
                settings.SUPABASE_BUCKET_RECORDINGS, key, payload, ctype
            )
            # 채점은 항상 무손실 원본 WAV 로(임시파일). 저장 key 는 stored(또는 None).
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
                f.write(data)
                tmp_path = f.name
            try:
                return ReviewService(db).add_review(
                    member.member_id, sentence_id,
                    ReviewCreate(voice_url=stored or None),
                    audio_override=tmp_path,
                )
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

        # ── [dev] DB 어드민 (로그인 필요) ──
        @app.get("/__admin", include_in_schema=False)
        def admin_page() -> FileResponse:
            """전체 DB 를 한눈에 보고 관리하는 어드민 HTML."""
            return FileResponse(
                Path(__file__).parent / "scripts" / "admin.html",
                media_type="text/html",
            )

        @app.get("/__dev/db/meta", include_in_schema=False)
        def admin_meta(member: CurrentMember, db: DbSession):  # type: ignore[no-untyped-def]
            from core import dev_admin
            return dev_admin.meta(db)

        @app.get("/__dev/db/rows", include_in_schema=False)
        def admin_rows(  # type: ignore[no-untyped-def]
            member: CurrentMember, db: DbSession,
            table: str, limit: int = 50, offset: int = 0,
        ):
            from core import dev_admin
            return dev_admin.rows(db, table, min(max(limit, 1), 500), max(offset, 0))

        @app.post("/__dev/db/delete", include_in_schema=False)
        def admin_delete(member: CurrentMember, db: DbSession, body: dict):  # type: ignore[no-untyped-def]
            from core import dev_admin
            n = dev_admin.delete_row(db, body.get("table"), body.get("pk") or {})
            return {"deleted": n}

        @app.post("/__dev/db/update", include_in_schema=False)
        def admin_update(member: CurrentMember, db: DbSession, body: dict):  # type: ignore[no-untyped-def]
            from core import dev_admin
            n = dev_admin.update_row(
                db, body.get("table"), body.get("pk") or {}, body.get("changes") or {}
            )
            return {"updated": n}

    return app


app = create_app()  # uvicorn main:app 호환
