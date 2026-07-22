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
from domains.learning.realtime.stt_session import run_pron_stt
from domains.learning.service import normalcall_service as svc

logger = logging.getLogger(__name__)
router = APIRouter()

_WS_CLOSE_POLICY_VIOLATION = 1008


@router.websocket("/calls/stream")
async def ws_call_stream(websocket: WebSocket) -> None:
    """노멀콜 음성 브리지 WebSocket 핸들러.

    🧒 이 함수는 통화의 '입구 문지기'다. run_call(실제 통화)에 들어가기 전에 세 가지를 한다:
      ① 신분 확인(인증), ② 서버 준비물 챙기기(genai_client/settings/DB), ③ 통화 본체 위임.

    🧒 왜 토큰을 '쿼리스트링(?token=...)'으로 받나: 일반 REST API 는 HTTP 헤더(Authorization)
      에 토큰을 담고 FastAPI 의 Depends 로 자동 검증한다. 그런데 브라우저·앱의 WebSocket 연결은
      표준상 요청 헤더를 자유롭게 못 붙인다. 그래서 WS 에선 헤더 대신 주소 뒤 ?token= 로
      토큰을 실어 보내고, 여기서 직접 verify_token 으로 검증한다(Depends 를 못 쓰는 이유).

    🧒 왜 app.state 에서 꺼내 쓰나: genai_client(구글 Live 클라)나 DB 세션 공장은 서버가
      켜질 때(lifespan) 딱 한 번 만들어 app.state 라는 '전역 사물함'에 넣어둔다. 통화가 올
      때마다 새로 만들면 느리고 낭비라, 여기선 사물함에서 이미 만들어둔 걸 꺼내 재사용한다.
    """
    token = websocket.query_params.get("token") or ""
    client = getattr(websocket.app.state, "genai_client", None)
    settings: Settings | None = getattr(websocket.app.state, "settings", None)
    session_factory = getattr(websocket.app.state, "session_factory", None)

    # 인증: Supabase 토큰 검증(네트워크 → threadpool).
    # 🧒 verify_token 은 네트워크를 타는 '동기(기다리는)' 함수라, 그냥 부르면 이벤트 루프
    #   전체가 멈춘다(다른 통화도 같이 멈춤). run_in_threadpool 로 별도 스레드에 던져 실행하는
    #   동안 루프는 다른 일을 계속한다. 검증 실패(토큰 없음·가짜)거나 서버 준비물(DB)이 없으면,
    #   accept 하지 않고 1008(정책 위반) 코드로 바로 닫는다 — 인증 안 된 소켓은 열지도 않는다.
    auth_user = await run_in_threadpool(verify_token, token) if token else None
    if auth_user is None or session_factory is None:
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_CLOSE_POLICY_VIOLATION)
        return

    # accept = 서버가 WS 연결을 정식 수락(악수 완료). 인증을 통과한 뒤에야 문을 연다.
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
        # 여기서부터 통화 본체(call_session.run_call)로 완전히 위임한다. 라우터는 문지기까지만.
        await run_call(websocket, settings, client, session_factory, member_id=member_id)
    except Exception as exc:  # noqa: BLE001 - 최종 방어선
        # 🧒 최종 방어선: run_call 안에서 어떤 예상 못 한 오류가 터져도 서버 전체가 흔들리지
        #   않게, 여기서 다 붙잡아 로그만 남기고 소켓을 정리한다(이 통화만 실패, 서버는 계속).
        logger.exception("ws_call_stream 처리 중 예외: %s", exc)
    finally:
        # 소켓이 아직 안 닫혔으면 닫는다(정상/예외 어느 경로든 소켓 누수 방지). 이미 끊긴 소켓에
        # close 하면 에러가 나므로 상태를 먼저 확인하고, 그래도 나는 예외는 조용히 삼킨다.
        if websocket.client_state != WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()


@router.websocket("/pron/stt/ws")
async def ws_pron_stt(websocket: WebSocket) -> None:
    """발음 챌린지 서버 STT WebSocket — 마이크 PCM(LINEAR16) → Google Speech 스트리밍 전사.

    계약(웹 발음챌린지와 동일):
      client→server: 첫 {"type":"config","words":[...],"sampleRate":N} → 바이너리 PCM → 선택 {"type":"stop"}
      server→client: {"type":"ready"|"partial"|"final"|"error", ...}

    인증(D1): Supabase 액세스 토큰(?token=) **필수** — normalcall WS 와 동일 규약.
      STT 는 과금이 있어 인증된 사용자만 허용한다. 토큰 없음/무효면 1008 로 즉시 닫는다.
    """
    token = websocket.query_params.get("token") or ""
    auth_user = await run_in_threadpool(verify_token, token) if token else None
    if auth_user is None:
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_CLOSE_POLICY_VIOLATION)
        return

    await websocket.accept()
    try:
        await run_pron_stt(websocket)
    except Exception as exc:  # noqa: BLE001 - 최종 방어선(이 세션만 실패, 서버는 계속)
        logger.exception("ws_pron_stt 처리 중 예외: %s", exc)
    finally:
        if websocket.client_state != WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()


@router.get("/calls/{call_id}/status")
def get_call_status(call_id: int, member: CurrentMember, db: DbSession) -> dict:
    """통화 분석 진행상태(ongoing/analyzing/done/failed) + 콜타입/판정 레벨.

    없거나 타인 통화면 status="unknown"(기존 하위호환) + 나머지 None.
    call_type="level_test" && status="done" 이면 assessed_level 이 판정 결과(1~13).
    """
    detail = svc.get_status_detail(db, call_id, member.member_id)
    if detail is None:
        return {"call_id": call_id, "status": "unknown", "call_type": None, "assessed_level": None}
    return {"call_id": call_id, **detail}
