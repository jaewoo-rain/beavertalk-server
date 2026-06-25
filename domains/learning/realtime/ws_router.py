"""normalcall realtime 라우터 — WS 통화 엔드포인트(인증/생명주기) + 분석 status 폴링.

WS /api/v1/calls/stream:
    - 인증: 쿼리스트링 `?token=<JWT access>` → decode_token → member_id. 실패 시 1008 close.
      (get_current_member 는 Depends(get_db)+oauth2_scheme 라 WS 에 그대로 못 쓰므로 별도.)
    - app.state 에서 genai_client / settings / session_factory 를 꺼내 call_session.run_call 위임.

GET /api/v1/calls/{call_id}/status:
    - 비동기 분석 진행상태 폴링(ongoing/analyzing/done/failed). 결과 본문은 기존
      GET /api/v1/calls/{call_id}/result(CallService) 로 조회.
"""

from __future__ import annotations

import contextlib
import logging

import jwt
from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketState

from core.config import Settings
from core.deps import CurrentMember, DbSession
from core.security import decode_token
from domains.learning.realtime.call_session import run_call
from domains.learning.realtime.protocol import ServerError, server_adapter
from domains.learning.service import normalcall_service as svc

logger = logging.getLogger(__name__)
router = APIRouter()

_WS_CLOSE_POLICY_VIOLATION = 1008


def _member_id_from_token(websocket: WebSocket) -> int | None:
    """쿼리 토큰을 검증해 member_id 를 반환한다(실패 시 None). deps 의 access 규칙과 동일."""
    token = websocket.query_params.get("token") or ""
    if not token:
        return None
    try:
        payload = decode_token(token)
        if payload.get("purpose") != "access":
            return None
        sub = payload.get("sub")
        if sub is None:
            return None
        return int(sub)
    except (jwt.PyJWTError, ValueError):
        return None


@router.websocket("/calls/stream")
async def ws_call_stream(websocket: WebSocket) -> None:
    """노멀콜 음성 브리지 WebSocket 핸들러."""
    member_id = _member_id_from_token(websocket)
    if member_id is None:
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_CLOSE_POLICY_VIOLATION)
        return

    await websocket.accept()

    client = getattr(websocket.app.state, "genai_client", None)
    settings: Settings | None = getattr(websocket.app.state, "settings", None)
    session_factory = getattr(websocket.app.state, "session_factory", None)

    if client is None or settings is None or session_factory is None:
        logger.error("normalcall WS: app.state 미준비(genai_client/settings/session_factory).")
        with contextlib.suppress(Exception):
            await websocket.send_text(
                server_adapter.dump_json(
                    ServerError(code="server_not_ready", message="서버가 준비되지 않았습니다.", recoverable=False)
                ).decode("utf-8")
            )
            await websocket.close()
        return

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
    """통화 분석 진행상태(ongoing/analyzing/done/failed). 없거나 타인 통화면 404 의미의 unknown."""
    status = svc.get_status(db, call_id, member.member_id)
    return {"call_id": call_id, "status": status or "unknown"}
