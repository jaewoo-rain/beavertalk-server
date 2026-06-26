"""normalcall realtime 라우터 — WS 통화 엔드포인트(인증/생명주기) + 분석 status 폴링.

WS /api/v1/calls/stream:
    - 인증: 쿼리스트링 `?token=<Supabase access token>` → supabase_auth.verify_token →
      auth uuid → member find-or-create → member_id. 실패 시 1008 close.
      (HTTPBearer Depends 는 WS 에 그대로 못 쓰므로 쿼리 토큰을 직접 검증.)
    - app.state 에서 genai_client / settings / session_factory 를 꺼내 run_call 위임.

GET /api/v1/calls/{call_id}/status: 분석 진행상태 폴링.
"""

from __future__ import annotations

import contextlib
import logging

from fastapi import APIRouter, WebSocket
from fastapi.concurrency import run_in_threadpool
from starlette.websockets import WebSocketState

from core.config import Settings
from core.deps import CurrentMember, DbSession
from core.supabase_auth import verify_token
from domains.account.service.member_service import MemberService
from domains.learning.realtime.call_session import run_call
from domains.learning.realtime.protocol import ServerError, server_adapter
from domains.learning.service import normalcall_service as svc

logger = logging.getLogger(__name__)
router = APIRouter()

_WS_CLOSE_POLICY_VIOLATION = 1008


@router.websocket("/calls/stream")
async def ws_call_stream(websocket: WebSocket) -> None:
    """노멀콜 음성 브리지 WebSocket 핸들러."""
    token = websocket.query_params.get("token") or ""
    client = getattr(websocket.app.state, "genai_client", None)
    settings: Settings | None = getattr(websocket.app.state, "settings", None)
    session_factory = getattr(websocket.app.state, "session_factory", None)

    # 인증: Supabase 토큰 검증(네트워크 → threadpool).
    auth_user = await run_in_threadpool(verify_token, token) if token else None
    if auth_user is None or session_factory is None:
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_CLOSE_POLICY_VIOLATION)
        return

    await websocket.accept()

    if client is None or settings is None:
        logger.error("normalcall WS: app.state 미준비(genai_client/settings).")
        with contextlib.suppress(Exception):
            await websocket.send_text(
                server_adapter.dump_json(
                    ServerError(code="server_not_ready", message="서버가 준비되지 않았습니다.", recoverable=False)
                ).decode("utf-8")
            )
            await websocket.close()
        return

    # auth uuid → member find-or-create → member_id (DB 는 threadpool).
    member_id = await svc.run_db(
        session_factory,
        lambda db: MemberService(db).find_or_create_by_auth(auth_user.uid, auth_user.email).member_id,
    )

    try:
        await run_call(websocket, settings, client, session_factory, member_id=member_id)
    except Exception as exc:  # noqa: BLE001 - 최종 방어선
        logger.exception("ws_call_stream 처리 중 예외: %s", exc)
    finally:
        if websocket.client_state != WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()


@router.get("/calls/{call_id}/status")
def get_call_status(call_id: int, member: CurrentMember, db: DbSession) -> dict:
    """통화 분석 진행상태(ongoing/analyzing/done/failed). 없거나 타인 통화면 unknown."""
    status = svc.get_status(db, call_id, member.member_id)
    return {"call_id": call_id, "status": status or "unknown"}
