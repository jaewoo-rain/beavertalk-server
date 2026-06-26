"""normalcall 단일 양방향 브리지 — 5분 한국어 통화 본체(async 오케스트레이션).

beavertalk 의 검증된 bridge.py(2펌프 + 시계워처 + asyncio.timeout 절대 백스톱 +
TaskGroup + barge-in off)를 이 프로젝트로 포팅. 차이:
    - DB 는 동기 SQLAlchemy → normalcall_service 를 run_db(스레드풀+짧은세션)로 호출.
    - 통화중 1분마다 누적 세그먼트를 점진 flush(긴 통화·크래시 내성). 종료 시 나머지 flush.
    - 페르소나/레벨/locale 은 통화 시작 전 1회 DB 조회해 평범한 값으로 넘긴다(ORM 반입 금지).

⛔ 불변: TaskGroup 2펌프 · asyncio.timeout 절대 백스톱 · barge-in off · _finish_call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from typing import AsyncContextManager, Callable, Optional

from google import genai
from sqlalchemy.orm import sessionmaker

from core.config import Settings
from core.gemini_live import DEFAULT_VOICE, LiveEvent, LiveSessionProtocol, open_session
from core.persona_prompt import SEED_OPENING, build_system_instruction
from domains.learning.service import normalcall_service as svc
from domains.learning.realtime.protocol import (
    ServerCallEnded,
    ServerInputTranscript,
    ServerMessage,
    ServerOutputTranscript,
    ServerPong,
    ServerTurnEnd,
    ServerTurnStart,
    client_adapter,
    server_adapter,
)

logger = logging.getLogger(__name__)

# ⚠️ 테스트값(원래 운영: DURATION=300, ABSOLUTE=330). 통화 길이 늘릴 때 함께 상향.
CALL_DURATION_S = 60.0          # 첫 발화부터 이 시간 경과 → 종료 시드 주입
ABSOLUTE_CALL_TIMEOUT_S = 90.0  # 어떤 이유로든 이 상한 넘으면 강제 종료
SEED_TO_HANGUP_S = 12.0         # 종료 시드 후 정상 종료 안 되면 강제 종료까지
PLAYBACK_DONE_WAIT_S = 2.0      # call_ended 후 playback_done ack 대기 상한
FLUSH_INTERVAL_S = 60.0         # 통화중 누적 세그먼트 점진 저장 주기(1분)
DEFAULT_CHARACTER_ID = 1        # start 에 character_id 없을 때 폴백(비비, 기본 무료)

_CLOSE_SEED = (
    "[시스템] 통화 시간이 다 됐다. 자연스럽게 핑계를 대고 따뜻하게 작별 인사 후 끝내라. 1~2문장."
)

SessionFactory = Callable[..., AsyncContextManager[LiveSessionProtocol]]

# 통화후 분석 task 강참조 보관소(GC 방지).
_analysis_tasks: set[asyncio.Task] = set()


def _new_turn_id() -> str:
    return uuid.uuid4().hex[:12]


async def _send_json(ws, message: ServerMessage) -> None:
    await ws.send_text(server_adapter.dump_json(message).decode("utf-8"))


class _CallState:
    """두 펌프가 공유하는 통화 상태(세그먼트 누적 + 시계 + 종료 플래그)."""

    __slots__ = (
        "turn_id", "call_start_ts", "should_close", "close_seed_sent", "seed_sent_ts",
        "playback_done_event", "segments", "persisted_count",
        "cur_user_pcm", "cur_user_text", "cur_beaver_pcm", "cur_beaver_text", "next_turn_index",
    )

    def __init__(self) -> None:
        self.turn_id: Optional[str] = None
        self.call_start_ts: Optional[float] = None
        self.should_close = False
        self.close_seed_sent = False
        self.seed_sent_ts: Optional[float] = None
        self.playback_done_event = asyncio.Event()
        self.segments: list[dict] = []
        self.persisted_count = 0  # 이미 DB 에 저장한 세그먼트 수(점진 flush 커서)
        self.cur_user_pcm = bytearray()
        self.cur_user_text: list[str] = []
        self.cur_beaver_pcm = bytearray()
        self.cur_beaver_text: list[str] = []
        self.next_turn_index = 0


class _ClientDisconnect(Exception):
    """클라 WS 종료 내부 신호."""


class _CallFinished(Exception):
    """통화 정상 종료(작별 후/백스톱) 내부 신호."""


def _flush_user_segment(state: _CallState) -> None:
    if not state.cur_user_pcm and not state.cur_user_text:
        return
    text = "".join(state.cur_user_text).strip()
    logger.info("👤 USER[t%d]: %s", state.next_turn_index, text or "(무음/전사없음)")
    state.segments.append(
        {"turn_index": state.next_turn_index, "role": "user", "text": text, "pcm": bytes(state.cur_user_pcm)}
    )
    state.next_turn_index += 1
    state.cur_user_pcm = bytearray()
    state.cur_user_text = []


def _flush_beaver_segment(state: _CallState) -> None:
    if not state.cur_beaver_pcm and not state.cur_beaver_text:
        return
    text = "".join(state.cur_beaver_text).strip()
    logger.info("🦫 BEAVER[t%d]: %s", state.next_turn_index, text or "(전사없음)")
    state.segments.append(
        {"turn_index": state.next_turn_index, "role": "beaver", "text": text, "pcm": bytes(state.cur_beaver_pcm)}
    )
    state.next_turn_index += 1
    state.cur_beaver_pcm = bytearray()
    state.cur_beaver_text = []


# --------------------------------------------------------------------------- #
# 진입점
# --------------------------------------------------------------------------- #
async def run_call(
    client_ws,
    settings: Settings,
    client: genai.Client,
    db_session_factory: sessionmaker,
    *,
    member_id: int,
    live_session_factory: SessionFactory | None = None,
) -> None:
    """노멀콜 단일 통화를 양방향 중계한다(인증은 ws_router 가 끝낸 뒤 호출).

    Args:
        client_ws: 이미 accept 된 FastAPI WebSocket.
        settings: 서버 설정.
        client: lifespan 의 genai.Client(app.state.genai_client).
        db_session_factory: app.state.session_factory(SQLAlchemy sessionmaker).
        member_id: 인증된 회원 id.
        live_session_factory: Live 세션 CM 팩토리(모킹 확장점). None 이면 호출 시점에
            모듈의 open_session 을 사용한다(기본 인자로 박지 않아 monkeypatch 가능).
    """
    # 기본값을 함수 정의 시점에 바인딩하지 않고 호출 시점에 해석 → 테스트에서
    # `open_session` 을 monkeypatch 하면 그대로 반영된다(운영은 실제 open_session).
    factory = live_session_factory or open_session
    # 1) 첫 start → character_id / locale override.
    try:
        character_id, locale_override = await _read_initial_start(client_ws)
    except _ClientDisconnect:
        logger.info("normalcall: start 수신 전 클라 종료")
        return

    # 2) 프롬프트 입력 조회(레벨 프로파일·페르소나·voice·locale) — 1회, 짧은 세션.
    setup = await svc.run_db(db_session_factory, lambda db: svc.load_call_setup(db, member_id, character_id))
    locale = locale_override or setup["locale"]

    system_instruction = build_system_instruction(
        role=setup["role"],
        personality=setup["personality"],
        rules=setup["rules"],
        level_profile=setup["level_profile"],
        locale=locale,
        interests=setup["interests"],
        name=setup["name"],
        history=None,
    )

    # 3) 통화 행 생성.
    call_id = await svc.run_db(
        db_session_factory, lambda db: svc.create_call(db, member_id, character_id)
    )

    state = _CallState()
    logger.info(
        "normalcall 시작: member=%s character=%s locale=%s voice=%s call_id=%s",
        member_id, character_id, locale, setup["voice"], call_id,
    )

    try:
        async with asyncio.timeout(ABSOLUTE_CALL_TIMEOUT_S):
            await _run_session(
                client_ws,
                state=state,
                system_instruction=system_instruction,
                voice=setup["voice"] or DEFAULT_VOICE,
                settings=settings,
                client=client,
                live_session_factory=factory,
                db_session_factory=db_session_factory,
                call_id=call_id,
                member_id=member_id,
            )
    except TimeoutError:
        logger.warning("normalcall 통화 상한(%.0fs) 초과 — 강제 종료", ABSOLUTE_CALL_TIMEOUT_S)
    except _ClientDisconnect:
        logger.info("normalcall 클라 연결 종료")
    except _CallFinished:
        logger.info("normalcall 통화 정상 종료")
    except Exception as exc:  # noqa: BLE001 - 최종 방어선
        logger.exception("normalcall 브리지 오류: %s", exc)
    finally:
        _flush_user_segment(state)
        _flush_beaver_segment(state)
        await _persist_remaining(db_session_factory, state, call_id, member_id)
        _trigger_analysis(call_id, client, settings, db_session_factory, locale)
        await _finish_call(client_ws, state, call_id)


def _trigger_analysis(call_id, client, settings, db_session_factory, locale) -> None:
    """통화후 분석을 백그라운드 task 로 띄운다(non-blocking, GC 방지 보관)."""
    task = asyncio.create_task(
        svc.analyze_call(call_id, client, settings, db_session_factory, locale=locale),
        name=f"normalcall-analysis-{call_id}",
    )
    _analysis_tasks.add(task)
    task.add_done_callback(_on_analysis_done)


def _on_analysis_done(task: asyncio.Task) -> None:
    _analysis_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("normalcall 분석 task 예외(무시): %s", exc)


async def _persist_remaining(db_session_factory, state: _CallState, call_id: int, member_id: int) -> None:
    """아직 저장 안 한 세그먼트를 일괄 저장 + 통화 종료 메타 갱신(graceful)."""
    new = state.segments[state.persisted_count:]
    duration_s = 0
    if state.call_start_ts is not None:
        duration_s = int(asyncio.get_running_loop().time() - state.call_start_ts)
    try:
        if new:
            await svc.run_db(
                db_session_factory, lambda db: svc.save_segments(db, call_id, new, member_id)
            )
            state.persisted_count += len(new)
        await svc.run_db(
            db_session_factory, lambda db: svc.finalize_call(db, call_id, total_time=duration_s, status="analyzing")
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("normalcall: 통화 저장 실패(무시): %s", exc)
    logger.info(
        "normalcall: 저장 완료 call_id=%s segments=%d duration=%ds",
        call_id, len(state.segments), duration_s,
    )


async def _run_session(
    client_ws,
    *,
    state: _CallState,
    system_instruction: str,
    voice: str,
    settings: Settings,
    client: genai.Client,
    live_session_factory: SessionFactory,
    db_session_factory: sessionmaker,
    call_id: int,
    member_id: int,
) -> None:
    """Live 세션 + 2펌프 + 시계워처 + 점진 flush 를 동시에 실행(타임아웃 안쪽)."""
    async with live_session_factory(
        client, settings, system_instruction=system_instruction, voice=voice
    ) as session:
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(_pump_client_to_gemini(client_ws, session, state), name="nc-client->gemini")
                tg.create_task(_pump_gemini_to_client(client_ws, session, state), name="nc-gemini->client")
                tg.create_task(_watch_call_clock(state), name="nc-clock")
                tg.create_task(
                    _periodic_flush(db_session_factory, state, call_id, member_id), name="nc-flush"
                )
                await session.send_text_turn(SEED_OPENING)  # 선톡 트리거
        except* _CallFinished:
            raise _CallFinished()
        except* _ClientDisconnect:
            raise _ClientDisconnect()


async def _periodic_flush(db_session_factory, state: _CallState, call_id: int, member_id: int) -> None:
    """통화중 FLUSH_INTERVAL_S 마다 누적 세그먼트를 점진 저장(긴 통화·크래시 내성)."""
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_S)
        new = state.segments[state.persisted_count:]
        if not new:
            continue
        target = state.persisted_count + len(new)
        try:
            await svc.run_db(
                db_session_factory, lambda db: svc.save_segments(db, call_id, new, member_id)
            )
            state.persisted_count = target
            logger.info("normalcall: 점진 flush %d개(누적 %d) call_id=%s", len(new), target, call_id)
        except Exception as exc:  # noqa: BLE001 - flush 실패는 다음 주기/종료시 재시도
            logger.warning("normalcall: 점진 flush 실패(무시): %s", exc)


async def _read_initial_start(client_ws) -> tuple[int, str | None]:
    """첫 start 에서 character_id / locale override 확보. 없으면 기본 캐릭터로 폴백."""
    from starlette.websockets import WebSocketDisconnect

    try:
        for _ in range(6):
            try:
                message = await asyncio.wait_for(client_ws.receive(), timeout=2.0)
            except asyncio.TimeoutError:
                break
            if message.get("type") == "websocket.disconnect":
                raise _ClientDisconnect()
            text = message.get("text")
            if text is not None:
                with contextlib.suppress(Exception):
                    cm = client_adapter.validate_python(json.loads(text))
                    if cm.type == "start":
                        return int(getattr(cm, "character_id", DEFAULT_CHARACTER_ID)), getattr(cm, "locale", None)
    except WebSocketDisconnect as exc:
        raise _ClientDisconnect() from exc
    return DEFAULT_CHARACTER_ID, None


# --------------------------------------------------------------------------- #
# 펌프: 클라 → Gemini
# --------------------------------------------------------------------------- #
async def _pump_client_to_gemini(client_ws, session: LiveSessionProtocol, state: _CallState) -> None:
    """클라 → Gemini. barge-in off: 비버 발화중이면 마이크 미전송. forward 먼저 후 누적."""
    from starlette.websockets import WebSocketDisconnect

    try:
        while True:
            message = await client_ws.receive()
            if message.get("type") == "websocket.disconnect":
                raise _ClientDisconnect()
            data = message.get("bytes")
            if data and state.turn_id is None:
                await session.send_audio(data)
                state.cur_user_pcm.extend(data)
                continue
            text = message.get("text")
            if text is not None:
                await _handle_client_control(client_ws, text, state)
                continue
    except WebSocketDisconnect as exc:
        raise _ClientDisconnect() from exc


async def _handle_client_control(client_ws, text: str, state: _CallState) -> None:
    try:
        msg = client_adapter.validate_python(json.loads(text))
    except Exception as exc:  # noqa: BLE001 - 미지/깨진 제어 무시
        logger.warning("normalcall 제어 메시지 무시: %s", exc)
        return
    if msg.type == "ping":
        await _send_json(client_ws, ServerPong(t=getattr(msg, "t", None)))
    elif msg.type == "playback_done":
        state.playback_done_event.set()


# --------------------------------------------------------------------------- #
# 펌프: Gemini → 클라
# --------------------------------------------------------------------------- #
async def _pump_gemini_to_client(client_ws, session: LiveSessionProtocol, state: _CallState) -> None:
    """Gemini → 클라(상태기계). 턴 경계에서 세그먼트 확정 + 5분 종료 로직."""
    event_count = 0
    async for event in session.events():
        event_count += 1
        turn_started = await _forward_event(client_ws, event, state)

        if turn_started:
            _flush_user_segment(state)  # 비버 발화 시작 → 직전 사용자 세그먼트 확정
            if state.call_start_ts is None:
                state.call_start_ts = asyncio.get_running_loop().time()
                logger.info("normalcall: 통화 시계 시작(첫 turn_start)")

        if event.kind == "turn_end":
            _flush_beaver_segment(state)
            if state.close_seed_sent:
                logger.info("normalcall: 종료 시드 응답 종료 → 종료 절차")
                raise _CallFinished()
            if state.should_close:
                state.close_seed_sent = True
                state.seed_sent_ts = asyncio.get_running_loop().time()
                await session.send_text_turn(_CLOSE_SEED)
                logger.info("normalcall: 경과 → 종료 시드 주입")

    logger.warning("normalcall: Live 이벤트 스트림 종료(서버측 close) events=%d", event_count)
    raise _CallFinished()


async def _forward_event(client_ws, event: LiveEvent, state: _CallState) -> bool:
    """단일 LiveEvent 를 즉시 forward 하며 진행중 세그먼트에 누적. 새 턴이면 True."""
    turn_started = False

    if event.kind == "audio":
        if state.turn_id is None:
            state.turn_id = _new_turn_id()
            await _send_json(client_ws, ServerTurnStart(turn_id=state.turn_id))
            turn_started = True
        if event.audio:
            await client_ws.send_bytes(event.audio)  # forward 먼저(반응성 우선)
            state.cur_beaver_pcm.extend(event.audio)

    elif event.kind == "in_tr":
        text = event.text or ""
        await _send_json(client_ws, ServerInputTranscript(text=text))
        if text:
            state.cur_user_text.append(text)
            logger.info("normalcall 👤 user: %s", text)

    elif event.kind == "out_tr":
        if state.turn_id is None:
            state.turn_id = _new_turn_id()
            await _send_json(client_ws, ServerTurnStart(turn_id=state.turn_id))
            turn_started = True
        text = event.text or ""
        await _send_json(client_ws, ServerOutputTranscript(text=text, turn_id=state.turn_id))
        if text:
            state.cur_beaver_text.append(text)
            logger.info("normalcall 🦫 beaver: %s", text)

    elif event.kind == "turn_end":
        turn_id = state.turn_id or _new_turn_id()
        await _send_json(client_ws, ServerTurnEnd(turn_id=turn_id))
        state.turn_id = None

    return turn_started


# --------------------------------------------------------------------------- #
# 통화 시계 워처 + 종료
# --------------------------------------------------------------------------- #
async def _watch_call_clock(state: _CallState) -> None:
    """경과 감시: should_close 플래그만 세우고(주입은 펌프 turn_end), 하드 백스톱 강제종료."""
    loop = asyncio.get_running_loop()
    while state.call_start_ts is None:
        await asyncio.sleep(0.2)
    while loop.time() - state.call_start_ts < CALL_DURATION_S:
        await asyncio.sleep(0.2)
    state.should_close = True
    logger.info("normalcall: %.0fs 경과 → 종료 플래그", CALL_DURATION_S)

    seed_wait_deadline = loop.time() + SEED_TO_HANGUP_S
    while not state.close_seed_sent and loop.time() < seed_wait_deadline:
        await asyncio.sleep(0.2)
    base = state.seed_sent_ts if state.seed_sent_ts is not None else loop.time()
    while loop.time() - base < SEED_TO_HANGUP_S:
        await asyncio.sleep(0.2)
    logger.warning("normalcall: 종료 백스톱 도달 → 강제 종료")
    raise _CallFinished()


async def _finish_call(client_ws, state: _CallState, call_id: int | None) -> None:
    """call_ended 송신 → playback_done ack 대기 → WS close(전부 graceful)."""
    from starlette.websockets import WebSocketState

    with contextlib.suppress(Exception):
        if client_ws.client_state == WebSocketState.CONNECTED:
            await _send_json(client_ws, ServerCallEnded(call_id=str(call_id or ""), reason="done"))
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(state.playback_done_event.wait(), timeout=PLAYBACK_DONE_WAIT_S)
    with contextlib.suppress(Exception):
        if client_ws.client_state != WebSocketState.DISCONNECTED:
            await client_ws.close()
