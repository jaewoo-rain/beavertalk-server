"""캐스케이드 통화 WS 라우터 — **dev 전용**(main.py 가 ENV != prod 일 때만 include).

WS /api/v1/cascade/stream?token=<Supabase access token>
  인증은 발음챌린지 STT WS 와 같은 규약이다 — STT 는 과금이 있으므로 인증된 사용자만.
  토큰 없음/무효면 accept 하지 않고 1008 로 닫는다.

normalcall(`/calls/stream`)과 **완전히 분리된 경로**다. 이 라우터는 통화 DB·분석·페르소나를
전혀 건드리지 않는다(P0 = 턴 감지 실증).
"""

from __future__ import annotations

import contextlib
import logging

from fastapi import APIRouter, WebSocket
from fastapi.concurrency import run_in_threadpool
from starlette.websockets import WebSocketState

from core.supabase_auth import verify_token
from domains.learning.realtime.cascade_session import run_cascade

logger = logging.getLogger(__name__)
router = APIRouter()

_WS_CLOSE_POLICY_VIOLATION = 1008


@router.websocket("/cascade/stream")
async def ws_cascade_stream(websocket: WebSocket) -> None:
    """캐스케이드 턴 감지 WS — 마이크 PCM16/16k → STT v2 → 턴 시작/종료 판정 에코."""
    token = websocket.query_params.get("token") or ""
    auth_user = await run_in_threadpool(verify_token, token) if token else None
    if auth_user is None:
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_CLOSE_POLICY_VIOLATION)
        return

    await websocket.accept()
    try:
        await run_cascade(websocket)
    except Exception as exc:  # noqa: BLE001 - 최종 방어선(이 세션만 실패, 서버는 계속)
        logger.exception("ws_cascade_stream 처리 중 예외: %s", exc)
    finally:
        if websocket.client_state != WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()
