"""캐스케이드 통화 WS 라우터 — **dev 전용**(main.py 가 ENV != prod 일 때만 include).

WS /api/v1/cascade/stream?token=<Supabase access token>
  인증은 발음챌린지 STT WS 와 같은 규약이다 — STT 는 과금이 있으므로 인증된 사용자만.
  토큰 없음/무효면 accept 하지 않고 1008 로 닫는다.

normalcall(`/calls/stream`)과 **완전히 분리된 경로**다. 이 라우터는 통화 DB·분석·페르소나를
전혀 건드리지 않는다(데모는 통화 기록·분석 없이 음성 왕복만 돈다).
"""

from __future__ import annotations

import contextlib
import logging

from fastapi import APIRouter, WebSocket
from fastapi.concurrency import run_in_threadpool
from starlette.websockets import WebSocketState

from core.supabase_auth import verify_token
from domains.account.service.member_service import MemberService
from domains.learning.realtime.cascade_session import run_cascade
from domains.learning.service import normalcall_service as svc

logger = logging.getLogger(__name__)
router = APIRouter()

_WS_CLOSE_POLICY_VIOLATION = 1008


@router.websocket("/cascade/stream")
async def ws_cascade_stream(websocket: WebSocket) -> None:
    """캐스케이드 턴 감지 WS — 마이크 PCM16/16k → STT v2 → 턴 시작/종료 판정 에코."""
    token = websocket.query_params.get("token") or ""
    session_factory = getattr(websocket.app.state, "session_factory", None)
    auth_user = await run_in_threadpool(verify_token, token) if token else None
    # ⛔ **회원을 못 풀면 통화를 안 연다**(Live 와 같은 규율). 누구 통화인지 모르면 기록도
    #   못 남기고, 남겨도 주인이 없다. DB 준비물이 없을 때도 같다.
    if auth_user is None or session_factory is None:
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_CLOSE_POLICY_VIOLATION)
        return

    await websocket.accept()

    # ⭐ **어느 클라에서 온 통화인가** — 접속 때 한 줄만 남긴다(2026-08-13).
    #   왜: "웹에서는 괜찮았는데 앱만 느리다"를 조사할 때 **로그로 못 갈랐다.** AEC 힌트는
    #   양쪽 다 `mode=unknown` 이라 구분이 안 됐고, 그래서 추측으로 답할 뻔했다. 이 한 줄이
    #   없어서 못 가른 것이므로 이 한 줄을 남긴다.
    #   ⚠ 판정하지 않는다 — **원문 그대로** 남기고 해석은 사람이 한다(우리가 웹/앱을 규칙으로
    #     찍으면 그 규칙이 틀렸을 때 조용히 틀린 답이 나온다). 브라우저는 origin 이 있고
    #     플러터 앱은 보통 없다 — 그 차이만으로 대개 갈린다.
    #   ⚠ UA 는 길 수 있어 잘라 남긴다. 개인정보가 아니라 클라 종류 식별용이다.
    headers = websocket.headers
    logger.info(
        "cascade 클라: origin=%s ua=%r",
        headers.get("origin") or "-", (headers.get("user-agent") or "-")[:120],
    )

    # auth uuid → member find-or-create → (member_id, 학습 대상 언어). DB 는 threadpool.
    # ⭐ **Live 와 같은 함수·같은 자리**다(`ws_router.ws_call_stream`). 두 경로가 다른 방식으로
    #   회원을 풀면 통화 기록의 주인이 갈린다.
    # ⚠ 학습 대상 언어를 여기서 같이 꺼내는 이유도 Live 와 같다 — 세션이 캐릭터·프롬프트를
    #   조회하기 **전에** 언어를 알아야 하는데, 그러자고 DB 를 한 번 더 타면 첫 발화가 늦어진다.
    try:
        member_id, member_target_language = await svc.run_db(
            session_factory,
            lambda db: (
                lambda m: (m.member_id, m.target_language)
            )(MemberService(db).find_or_create_by_auth(auth_user.uid, auth_user.email)),
        )
    except Exception as exc:  # noqa: BLE001 - 회원 해석 실패는 통화를 열지 않는다
        logger.warning("cascade WS: 회원 해석 실패 — 통화를 열지 않는다: %s", str(exc)[:200])
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_CLOSE_POLICY_VIOLATION)
        return
    try:
        # LLM 클라이언트는 서버가 켜질 때 만들어 app.state 에 둔다(normalcall 과 같은 규율).
        # 없으면 비버는 말하지 않고 **턴 감지만** 돈다 — 키 부재로 통화가 죽지 않는다(R5).
        await run_cascade(
            websocket,
            getattr(websocket.app.state, "genai_client", None),
            # ⭐ 대답 LLM 은 **리전이 다를 수 있다**(`CASCADE_LLM_LOCATION`). 없으면 위와 같은
            #   객체가 온다 — 기본 동작 불변. ⚠ 힌트·분석은 위 클라이언트를 계속 쓴다.
            llm_client=getattr(websocket.app.state, "cascade_llm_client", None),
            # ⚠ **실제로 만들어진** 리전(설정값이 아니다) — 통화 로그가 이 값을 찍는다.
            llm_location=getattr(websocket.app.state, "cascade_llm_location", "") or "",
            session_factory=session_factory,
            member_id=member_id,
            member_target_language=member_target_language,
        )
    except Exception as exc:  # noqa: BLE001 - 최종 방어선(이 세션만 실패, 서버는 계속)
        logger.exception("ws_cascade_stream 처리 중 예외: %s", exc)
    finally:
        if websocket.client_state != WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()
