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
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.config import Settings
from core.config import settings as default_settings
from core.deps import CurrentMember, DbSession
from db.engine import build_engine
from db.session import build_session_factory
from domains.account.routers import router as account_router
from domains.alarm.routers import router as alarm_router
from domains.commerce.routers import router as commerce_router
from domains.learning.routers import router as learning_router
from domains.push.routers import router as push_router

API_PREFIX = "/api/v1"


def _configure_logging() -> None:
    """통화(normalcall) 로그만 stdout 에 노출(전역 INFO 는 건드리지 않음).

    파이썬 루트 로거는 기본 WARNING 이라 앱 모듈의 logger.info 가 버려진다(Cloud Run 이
    숨기는 게 아님 — 파이썬 기본값). 전역을 통째로 INFO 로 올리는 대신, normalcall 실시간
    패키지 로거에만 INFO StreamHandler 를 달아 통화 전사(👤/🦫)·genai 흐름만 보이게 한다.
    propagate=False 로 루트로 전파하지 않아 다른 로그 노이즈/비용 증가가 없다.
    """
    handler = logging.StreamHandler()  # stdout → Cloud Logging
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    for name in ("domains.learning.realtime", "domains.push"):  # 통화 WS/세션 + 예약전화 발송
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.addHandler(handler)
        lg.setLevel(logging.INFO)
        lg.propagate = False


_configure_logging()

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
    from core import fcm

    fcm.warmup()  # 예약전화 FCM 워밍업(실패해도 무시 — 발송만 비활성)
    try:
        yield
    finally:
        engine.dispose()  # 종료 시 커넥션 정리


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """HTTPException → 표준 에러 바디로 통일.

    raise HTTPException(409, "이미 가입된 이메일입니다.") 처럼 문자열만 줘도
    {"detail": {"code": "HTTP_409", "message": "..."}} 로 감싼다.
    이미 dict(detail) 를 준 경우는 그대로 통과.

    ⚠️ Starlette 의 HTTPException(FastAPI HTTPException 의 부모)으로 등록한다 — 그래야
    우리가 raise 한 것뿐 아니라 프레임워크가 내부에서 던지는 것(없는 라우트 404, 405 등)까지
    한 포맷으로 잡힌다(공식 권장). FastAPI HTTPException 은 이것의 하위라 함께 잡힌다.
    """
    detail = exc.detail
    if isinstance(detail, dict):
        body = {"detail": detail}
    else:
        body = {"detail": {"code": f"HTTP_{exc.status_code}", "message": str(detail)}}
    return JSONResponse(
        status_code=exc.status_code, content=body, headers=getattr(exc, "headers", None)
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """요청 검증 실패(422) → HTTPException 과 같은 표준 바디로 통일.

    FastAPI 기본 422 는 {"detail": [필드에러...]} 라 위 HTTPException 포맷과 모양이 달라
    클라가 두 형태를 따로 다뤄야 한다. 여기서 {"detail": {"code","message"}} 로 감싸 통일하고,
    필드별 상세는 errors 로 함께 준다(프론트가 어느 필드가 틀렸는지 쓸 수 있게).
    """
    errors = jsonable_encoder(exc.errors())
    first = errors[0] if errors else {}
    loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    msg = first.get("msg", "요청 값이 올바르지 않습니다.")
    message = f"{loc}: {msg}" if loc else msg
    return JSONResponse(
        status_code=422,
        content={"detail": {"code": "VALIDATION_ERROR", "message": message, "errors": errors}},
    )


async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """어디서도 못 잡은 예외 → 500(최후의 그물). 추적 ID 만 클라에 주고 상세는 서버 로그로.

    왜: 예상 못 한 버그의 스택트레이스·내부 메시지를 그대로 응답에 실으면 민감정보가 샌다.
    그래서 클라엔 짧은 안내 + error_ref_id(랜덤 ID)만 주고, 서버 로그에 같은 ID로 전체 예외를
    남긴다 — 사용자가 ID 를 알려주면 로그에서 바로 그 사건을 찾는다(장애 추적).
    HTTPException·검증에러는 각자 핸들러가 먼저 잡으니, 여기는 '진짜 예기치 못한' 것만 온다.
    """
    ref = uuid.uuid4().hex
    logger.exception(
        "unhandled exception ref=%s %s %s", ref, request.method, request.url.path
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "code": "INTERNAL_ERROR",
                "message": "서버 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
                "error_ref_id": ref,
            }
        },
    )


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

    # 예외 핸들러 3종(구체 → 일반 순서로 이해하면 됨 — Starlette 는 예외 타입 MRO 로 매칭).
    #   ① HTTPException(우리가 raise 한 것 + 프레임워크 내부 것) → 표준 바디
    #   ② 요청 검증 실패(422) → 같은 표준 바디로 통일
    #   ③ 그 외 모든 미처리 예외 → 500 + 추적 ID(최후의 그물)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        """헬스체크 (DB 연결은 확인하지 않음)."""
        return {"status": "ok", "env": settings.ENV}

    # ── 도메인 라우터 등록 ──
    app.include_router(account_router, prefix=API_PREFIX)
    app.include_router(commerce_router, prefix=API_PREFIX)
    app.include_router(alarm_router, prefix=API_PREFIX)
    app.include_router(learning_router, prefix=API_PREFIX)
    app.include_router(push_router, prefix=API_PREFIX)

    # ── (dev 전용) 통화 데모 콘솔 ──
    # 운영(prod)에는 노출하지 않는다. 같은 오리진으로 서빙하므로 CORS 불필요.
    if settings.ENV != "prod":

        @app.get("/__calldemo", include_in_schema=False)
        def call_demo() -> FileResponse:
            """통화→문장추출→복습 발음평가 전 과정 데모 HTML."""
            return FileResponse(
                Path(__file__).parent / "scripts" / "call_demo.html",
                media_type="text/html",
            )

        @app.post("/__dev/level-reset", include_in_schema=False)
        def dev_level_reset(member: CurrentMember, db: DbSession) -> dict:
            """[dev] 레벨 관련 상태 완전 초기화 — 재테스트용 백지화.

            korean_level=NULL + 체크판(progress)/증거(evidence)/승급 이력(history) 삭제.
            call 행(통화·판정 이력)은 보존한다(감사 기록 — 라우팅에 영향 없음).
            """
            from sqlalchemy import delete

            from domains.learning.models.item_evidence import ItemEvidence
            from domains.learning.models.member_item_progress import MemberItemProgress
            from domains.learning.models.member_level_history import MemberLevelHistory

            mid = member.member_id
            ev = db.execute(delete(ItemEvidence).where(ItemEvidence.member_id == mid)).rowcount
            pg = db.execute(
                delete(MemberItemProgress).where(MemberItemProgress.member_id == mid)
            ).rowcount
            hi = db.execute(
                delete(MemberLevelHistory).where(MemberLevelHistory.member_id == mid)
            ).rowcount
            member.korean_level = None
            db.commit()
            return {"korean_level": None, "deleted": {"evidence": ev, "progress": pg, "history": hi}}

        @app.get("/__levelcalldemo", include_in_schema=False)
        def level_call_demo() -> FileResponse:
            """레벨테스트·멀티랭귀지 통화 데모 HTML — 판정·저장까지 실동작(레벨을 실제로 덮어씀).

            (멀티랭귀지) 학습 언어(target) 드롭다운으로 target_language 코드(ja 등)를 보낸다 —
            지원 언어면 그 언어의 정식 코스(레벨테스트→체크판→레벨업)로 잡힌다.
            '재측정 강제' 체크 시 call_type=level_test 명시(비프로드 전용 통로)로 반복 테스트 가능.
            """
            return FileResponse(
                Path(__file__).parent / "scripts" / "level_call_demo.html",
                media_type="text/html",
            )

        @app.post("/__dev/pron-eval", include_in_schema=False)
        async def dev_pron_eval(  # type: ignore[no-untyped-def]
            member: CurrentMember,
            db: DbSession,
            sentence_id: int = Form(...),
            audio: UploadFile = File(...),
        ):
            """[dev] 브라우저 녹음(WAV) → 채점. 프로덕션 로직(add_review_from_audio) 재사용.

            실제 클라이언트는 `POST /api/v1/sentences/{id}/reviews/audio` 를 쓴다.
            """
            from domains.learning.service.review_service import ReviewService

            raw = await audio.read()
            return ReviewService(db).add_review_from_audio(
                member.member_id, sentence_id, raw, audio.content_type
            )

        @app.get("/__dev/call-prompt", include_in_schema=False)
        def dev_call_prompt(  # type: ignore[no-untyped-def]
            member: CurrentMember,
            db: DbSession,
            character_id: int,
            target_language: str = "한국어",
            locale: str | None = None,
        ):
            """[dev] 통화 시작 시 서버가 조립하는 system_instruction + 구성요소 미리보기.

            call_session.run_call 과 동일하게 load_call_setup → build_system_instruction.
            target_language/locale 로 데모(프랑스어 등) 프롬프트도 미리 볼 수 있다(데모 게이트와 동일:
            대상 언어가 한국어가 아니면 level_profile 을 비우고 ko→"한국어" 라벨을 적용).
            """
            from core.persona_prompt import build_system_instruction
            from domains.learning.service import normalcall_service as nsvc

            setup = nsvc.load_call_setup(db, member.member_id, character_id)
            loc = locale or setup["locale"]
            is_demo = target_language != "한국어"
            locale_label = {"ko": "한국어"}.get(loc) if is_demo else None
            level_profile = "" if is_demo else setup["level_profile"]
            system_instruction = build_system_instruction(
                role=setup["role"],
                personality=setup["personality"],
                level_profile=level_profile,
                locale=loc,
                interests=setup["interests"],
                name=setup["name"],
                history=setup["history"],
                target_language=target_language,
                locale_label=locale_label,
                lang_band=setup.get("lang_band", "beginner"),
            )
            return {
                "setup": {**setup, "target_language": target_language, "locale": loc},
                "system_instruction": system_instruction,
            }

    return app


app = create_app()  # uvicorn main:app 호환
