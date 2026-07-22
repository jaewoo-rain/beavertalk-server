"""normalcall 실시간 음성통화 결정적 테스트 (외부 의존 0).

검증 대상:
    - ws_router: 쿼리 토큰 인증(없음/위조/유효), GET /calls/{id}/status 소유자 가드.
    - call_session.run_call: 가짜 Live 세션 + 가짜 WS + 인메모리 DB 로 통화 1회 끝까지 →
      Call.status 전이, CallRawData(role/turn_index/content) 생성, _CallFinished 즉시 종료.
    - normalcall_service: load_call_setup / save_segments / get_status / analyze_call.

모든 외부(Gemini Live·Gemini 분석·TTS·Storage·DB)는 인메모리/모킹. 60초 타이머는
가짜 events 를 즉시 turn_end 후 소진시켜 _CallFinished 로 바로 빠져나가게 해 회피한다.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# --- DB 시드용 모델 + 레지스트리(전 모델 등록 보장) ---
from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.account.models.member_reason import MemberReason
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.models.level import Level
from domains.learning.models.sentence import Sentence

from core.config import settings as app_settings
from core.supabase_auth import AuthUser

import core.deps as deps
import domains.learning.service.normalcall_service as svc
import domains.learning.realtime.call_session as cs
import domains.learning.realtime.ws_router as ws_router
from domains.learning.realtime.call_session import run_call
from core.gemini_live import LiveEvent


# 인증: Supabase 토큰 검증을 모킹 — Bearer 토큰 문자열 == auth uuid 로 취급.
# (빈 토큰/"bad" → None = 인증 실패)
def _fake_verify(token):
    if token and token.startswith("auth-"):  # 유효 테스트 토큰 = "auth-*"
        return AuthUser(uid=token, email=f"{token}@test.io")
    return None  # 빈 토큰/위조("not-a-jwt" 등) → 인증 실패


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(ws_router, "verify_token", _fake_verify)
    monkeypatch.setattr(deps, "verify_token", _fake_verify)


# --------------------------------------------------------------------------- #
# 인메모리 DB (BigInteger+Identity PK 는 sqlite 에서 autoincrement 안 되므로 Integer 로 치환)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def session_factory():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def seeded(session_factory):
    """Voice/Character/Level/Member 한 건씩 시드. ids 를 반환."""
    db = session_factory()
    try:
        voice = Voice(name="Fenrir", gender="male")
        db.add(voice)
        db.flush()
        ch = Character(name="비비", role="친근한 선생님", personality="다정함",
                       voice_id=voice.voice_id, price=0)
        db.add(ch)
        db.add(Level(level_no=1, profile="초급 학습자"))
        db.flush()
        member = Member(language="en", korean_level=1, onboarding_completed=True,
                        auth_user_id="auth-member")
        db.add(member)
        db.flush()
        # 흥미는 member_reason(온보딩 학습이유)에서 온다 → travel → "여행"
        db.add(MemberReason(member_id=member.member_id, reason="travel"))
        db.commit()
        return {"member_id": member.member_id, "character_id": ch.character_id,
                "voice": voice.name, "auth": "auth-member"}
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _mock_external(monkeypatch):
    """Storage/TTS/Gemini 분석을 결정적 스텁으로 — 네트워크 0."""
    monkeypatch.setattr(svc.storage, "upload", lambda *a, **k: "stub-key")
    monkeypatch.setattr(svc.storage, "public_url", lambda *a, **k: "https://stub/url.mp3")

    async def _fake_tts(*_a, **_k):
        return None

    monkeypatch.setattr(svc.tts, "synthesize", _fake_tts)

    async def _fake_generate(*_a, **_k):
        return svc.CallAnalysis(
            summary="짧은 통화 요약",
            detected_mode="chat",
            expressions=[
                svc.LearnedExpression(
                    korean="안녕하세요", translation="hi", source_type="asked"
                )
            ],
        )

    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", _fake_generate)


# --------------------------------------------------------------------------- #
# 가짜 WebSocket / 가짜 Live 세션
# --------------------------------------------------------------------------- #
class FakeWebSocket:
    """starlette WebSocket 인터페이스 일부를 흉내내는 가짜.

    receive(): 스크립트된 메시지 dict 를 순서대로 반환, 소진되면 disconnect.
    send_text/send_bytes: 송신 기록. close(): client_state 갱신.
    """

    def __init__(self, incoming: list[dict], hang: bool = False):
        self._incoming = list(incoming)
        self._hang = hang  # True 면 incoming 소진 후 disconnect 대신 영원히 대기(소강 재현)
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closed = False
        self.close_code = None
        # starlette WebSocketState.CONNECTED == 1, DISCONNECTED == 2
        from starlette.websockets import WebSocketState
        self._WS = WebSocketState
        self.client_state = WebSocketState.CONNECTED

    async def receive(self) -> dict:
        if self._incoming:
            return self._incoming.pop(0)
        if self._hang:  # 클라 발화 없는 소강 구간 재현 — 취소될 때까지 대기
            await asyncio.Event().wait()
        return {"type": "websocket.disconnect"}

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def close(self, code: int | None = None) -> None:
        self.closed = True
        self.close_code = code
        self.client_state = self._WS.DISCONNECTED


class FakeLiveSession:
    """LiveSessionProtocol 구현 — 스크립트된 한 턴 후 events 소진(자연 종료)."""

    def __init__(self):
        self.sent_audio: list[bytes] = []
        self.sent_text_turns: list[str] = []

    async def send_audio(self, pcm16_16k: bytes) -> None:
        self.sent_audio.append(pcm16_16k)

    async def send_text_turn(self, text: str) -> None:
        self.sent_text_turns.append(text)

    async def events(self):
        # 비버 한 턴: out_tr → audio → turn_end. 이후 종료 → _CallFinished.
        yield LiveEvent(kind="out_tr", text="Hi, 공부할래?")
        yield LiveEvent(kind="audio", audio=b"\x00\x00" * 8)
        yield LiveEvent(kind="turn_end")
        # 제너레이터 종료 → _pump_gemini_to_client 가 _CallFinished 발생


def make_live_factory(session_holder):
    """run_call 에 주입할 live_session_factory(async CM 팩토리)."""
    import contextlib

    @contextlib.asynccontextmanager
    async def _factory(client, settings, *, system_instruction, voice):
        sess = FakeLiveSession()
        session_holder["session"] = sess
        session_holder["system_instruction"] = system_instruction
        session_holder["voice"] = voice
        yield sess

    return _factory


async def _wait_analysis_tasks():
    """run_call 이 띄운 백그라운드 분석 task 가 끝날 때까지 대기."""
    for _ in range(200):
        if not cs._analysis_tasks:
            return
        await asyncio.sleep(0.01)


# --------------------------------------------------------------------------- #
# (a) WS 인증 — TestClient
# --------------------------------------------------------------------------- #
def _build_app(session_factory):
    from main import create_app
    app = create_app()
    app.state.session_factory = session_factory
    app.state.settings = app_settings
    app.state.genai_client = object()  # 분석은 모킹되므로 더미면 충분
    return app


def test_ws_rejects_without_token(session_factory):
    """토큰 없으면 1008 로 close → 핸드셰이크 실패(WebSocketDisconnect)."""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    app = _build_app(session_factory)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/calls/stream"):
            pass


def test_ws_rejects_invalid_token(session_factory):
    """위조 토큰도 인증 실패로 close."""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    app = _build_app(session_factory)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/calls/stream?token=not-a-jwt"):
            pass


def test_ws_accepts_valid_token_then_handles_call(session_factory, seeded, monkeypatch):
    """유효 토큰이면 핸드셰이크 accept 후 통화 진행(실 ASGI WS 경로).

    ws_router 가 run_call 을 live_session_factory 없이 호출하므로, ws_router 모듈의
    run_call 심볼을 래퍼로 monkeypatch 해 가짜 Live 세션을 주입한다(60초 타이머 회피).
    call_ended 를 받으면 즉시 루프를 멈춰(서버 close 와의 receive 교착 방지) 통신을 끝낸다.
    """
    from fastapi.testclient import TestClient
    import domains.learning.realtime.ws_router as wr

    holder: dict = {}
    fake_factory = make_live_factory(holder)

    async def _run_call_with_fake(*args, **kwargs):
        kwargs.setdefault("live_session_factory", fake_factory)
        return await cs.run_call(*args, **kwargs)

    monkeypatch.setattr(wr, "run_call", _run_call_with_fake)

    app = _build_app(session_factory)
    token = seeded["auth"]
    client = TestClient(app)
    received: list[dict] = []
    with client.websocket_connect(f"/api/v1/calls/stream?token={token}") as ws:
        ws.send_text(json.dumps({"type": "start",
                                 "character_id": seeded["character_id"]}))
        try:
            for _ in range(20):
                msg = ws.receive()
                received.append(msg)
                txt = msg.get("text")
                if txt and json.loads(txt).get("type") == "call_ended":
                    break  # 종료 통지 수신 → 서버 close 전 루프 종료(교착 방지)
        except Exception:
            pass

    types_seen = [json.loads(m["text"]).get("type")
                  for m in received if m.get("text")]
    assert "turn_start" in types_seen
    assert "call_ended" in types_seen


# --------------------------------------------------------------------------- #
# (b) run_call 직접 호출 — 통화 종료 후 DB 상태/세그먼트
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_run_call_persists_segments_and_status(session_factory, seeded):
    holder: dict = {}
    ws = FakeWebSocket([
        {"type": "websocket.receive",
         "text": json.dumps({"type": "start", "character_id": seeded["character_id"]})},
        # 사용자 오디오 한 청크(짝수 바이트)
        {"type": "websocket.receive", "bytes": b"\x01\x02\x03\x04"},
    ])

    await run_call(
        ws,
        app_settings,
        object(),  # genai client(분석 모킹) — 아무 객체
        session_factory,
        member_id=seeded["member_id"],
        live_session_factory=make_live_factory(holder),
    )
    await _wait_analysis_tasks()

    db = session_factory()
    try:
        calls = db.query(Call).all()
        assert len(calls) == 1
        call = calls[0]
        # 분석까지 끝나면 done, 분석 전이면 analyzing. 둘 다 허용(타이밍).
        assert call.status in ("analyzing", "done")
        assert call.member_id == seeded["member_id"]

        rows = db.query(CallRawData).order_by(CallRawData.turn_index).all()
        assert len(rows) >= 1
        roles = {r.role for r in rows}
        assert "beaver" in roles  # 비버 발화 세그먼트 확정됨
        beaver = next(r for r in rows if r.role == "beaver")
        assert beaver.content == "Hi, 공부할래?"
        assert beaver.turn_index is not None
    finally:
        db.close()

    # 가짜 Live 에 선톡 시드가 전송됐는지(send_text_turn) 확인
    assert holder["session"].sent_text_turns  # SEED_OPENING 주입됨
    assert holder["voice"] == seeded["voice"]  # 캐릭터 voice 가 반영됨


@pytest.mark.asyncio
async def test_auto_close_injects_seed_when_idle(session_factory, seeded, monkeypatch):
    """RC1 회귀: 5분 경과가 소강(idle) 구간에 떨어져도 종료 시드가 주입되고 정상 작별 종료.

    수정 전엔 시드 주입이 펌프의 turn_end 에만 걸려 있어, 첫 턴 후 비버가 idle 이면 turn_end 가
    안 와서 시드가 영영 안 나가고 무음 백스톱으로 뚝 끊겼다. 이제 워처가 idle 을 감지해 직접 주입한다.
    """
    monkeypatch.setattr(cs, "CALL_DURATION_S", 0.3)   # 5분 → 0.3초로 축소(빠른 테스트)
    monkeypatch.setattr(cs, "SEED_TO_HANGUP_S", 3.0)

    class IdleThenClose:
        """첫 턴 후 idle → 종료 시드([시스템]) 수신 시에만 작별 턴 → 종료."""

        def __init__(self):
            self.sent_audio: list[bytes] = []
            self.sent_text_turns: list[str] = []
            self._closed = asyncio.Event()

        async def send_audio(self, pcm16_16k: bytes) -> None:
            self.sent_audio.append(pcm16_16k)

        async def send_text_turn(self, text: str) -> None:
            self.sent_text_turns.append(text)
            if text.startswith("[시스템]"):  # 종료 시드 수신 → 작별 턴 방출 허용
                self._closed.set()

        async def events(self):
            yield LiveEvent(kind="out_tr", text="안녕")
            yield LiveEvent(kind="audio", audio=b"\x00\x00")
            yield LiveEvent(kind="turn_end")
            await self._closed.wait()          # idle(소강) — 종료 시드가 올 때까지 대기
            yield LiveEvent(kind="out_tr", text="잘 가요")
            yield LiveEvent(kind="audio", audio=b"\x22\x22")  # 작별 오디오
            yield LiveEvent(kind="turn_end")

    import contextlib as _cl
    holder: dict = {}

    @_cl.asynccontextmanager
    async def factory(client, settings, *, system_instruction, voice):
        s = IdleThenClose()
        holder["s"] = s
        yield s

    # 클라는 start 만 보내고 이후 침묵(hang=True) — 종료를 서버(시드 경로)가 주도해야 한다.
    ws = FakeWebSocket(
        [{"type": "websocket.receive",
          "text": json.dumps({"type": "start", "character_id": seeded["character_id"]})}],
        hang=True,
    )

    await run_call(
        ws, app_settings, object(), session_factory,
        member_id=seeded["member_id"], live_session_factory=factory,
    )
    await _wait_analysis_tasks()

    sess = holder["s"]
    # 소강에도 종료 시드가 주입됐다(워처가 직접) — 작별 없는 무음 종료 방지.
    assert any(t.startswith("[시스템]") for t in sess.sent_text_turns), \
        "소강 구간에서 종료 시드가 주입되지 않음(RC1 회귀)"
    # 작별 오디오가 클라로 forward 됐다(작별 절단 아님).
    assert b"\x22\x22" in ws.sent_bytes, "작별 오디오가 클라에 전달되지 않음"
    # 정상 종료 통지(call_ended)가 나갔다.
    assert any('"call_ended"' in t for t in ws.sent_text), "call_ended 미전송"


@pytest.mark.asyncio
async def test_close_seed_deferred_until_user_reply(session_factory, seeded, monkeypatch):
    """종료 레이스 회귀(call 197): 5분 직전에 유저가 말하면 비버의 '유저 응답'이 작별로
    둔갑하지 않고, 비버가 종료 시드에 '진짜 작별'을 한 뒤 종료한다.

    수정 전 결함: 워처가 '유저 발화 끝~비버 응답 시작' 빈틈(turn_id None·user_turn_open True)에
    시드를 주입 → 비버의 유저 응답 턴이 close_reply_started 로 오설정 → 작별 없이 즉시 종료.
    수정 후: user_turn_open 이면 워처가 양보 → 비버가 유저에 먼저 응답하고, 그 turn_end 에서
    펌프(should_close 경로)가 깨끗한 idle 에 시드 주입 → 비버가 시드에 작별.
    """
    monkeypatch.setattr(cs, "CALL_DURATION_S", 0.3)   # 5분 → 0.3초로 축소
    monkeypatch.setattr(cs, "SEED_TO_HANGUP_S", 3.0)
    monkeypatch.setattr(cs, "REGROUND_MODE", "off")   # 재접지 격리(종료만 검증)

    class UserSpeaksThenClose:
        """오프닝 후 유저가 말하고(=user_turn_open), 그 사이 5분 경과. 비버가 유저에 응답 →
        그 뒤에야 시드가 오고, 시드에 진짜 작별 턴을 방출한다."""

        def __init__(self):
            self.sent_text_turns: list[str] = []
            self._seeded = asyncio.Event()

        async def send_audio(self, pcm16_16k: bytes) -> None:
            pass

        async def send_text_turn(self, text: str) -> None:
            self.sent_text_turns.append(text)
            if text.startswith("[시스템]"):  # 종료 시드 수신 → 작별 턴 허용
                self._seeded.set()

        async def send_reground(self, text: str, *, turn_complete: bool = True) -> None:
            pass

        async def events(self):
            # 오프닝 비버 턴(여기서 통화 시계 시작)
            yield LiveEvent(kind="out_tr", text="안녕하세요")
            yield LiveEvent(kind="audio", audio=b"\x00\x00")
            yield LiveEvent(kind="turn_end")
            # 유저가 5분 직전 말함 → user_turn_open=True
            yield LiveEvent(kind="in_tr", text="네", is_final=True)
            # 이 사이 0.3초(=5분)가 지나 종료 플래그가 뜬다. 워처는 유저 응답 대기 중이라
            # 시드를 넣으면 안 된다(수정 전엔 여기서 넣어 다음 턴이 작별로 둔갑).
            await asyncio.sleep(0.6)
            # 비버가 '유저'에 응답(작별 아님) — 수정 전엔 이 턴이 작별로 오인돼 종료됨
            yield LiveEvent(kind="out_tr", text="그렇군요")
            yield LiveEvent(kind="audio", audio=b"\x11\x11")   # 유저 응답 오디오
            yield LiveEvent(kind="turn_end")
            # 이제(깨끗한 idle) 펌프가 시드 주입 → 진짜 작별 방출
            await self._seeded.wait()
            yield LiveEvent(kind="out_tr", text="잘 가요")
            yield LiveEvent(kind="audio", audio=b"\x33\x33")   # 작별 오디오 마커
            yield LiveEvent(kind="turn_end")

    import contextlib as _cl
    holder: dict = {}

    @_cl.asynccontextmanager
    async def factory(client, settings, *, system_instruction, voice):
        s = UserSpeaksThenClose()
        holder["s"] = s
        yield s

    ws = FakeWebSocket(
        [{"type": "websocket.receive",
          "text": json.dumps({"type": "start", "character_id": seeded["character_id"]})}],
        hang=True,
    )

    await run_call(
        ws, app_settings, object(), session_factory,
        member_id=seeded["member_id"], live_session_factory=factory,
    )
    await _wait_analysis_tasks()

    sess = holder["s"]
    # 종료 시드는 주입됐다(정상 종료).
    assert any(t.startswith("[시스템]") for t in sess.sent_text_turns), "종료 시드 미주입"
    # 비버의 '유저 응답' 오디오가 전달됐다(정상 대화).
    assert b"\x11\x11" in ws.sent_bytes, "비버의 유저 응답이 전달되지 않음"
    # ⭐ 핵심: 진짜 작별 오디오가 전달됐다 — 유저 응답이 작별로 둔갑해 잘리지 않았다(레이스 회귀).
    assert b"\x33\x33" in ws.sent_bytes, "진짜 작별 발화가 잘림(종료 레이스 회귀 — call 197)"


def test_absolute_backstop_is_540s():
    """A3: 연결 ~10분 한계를 선점하도록 절대 백스톱이 540s(9분)로 하향됐다."""
    assert cs.ABSOLUTE_CALL_TIMEOUT_S == 540.0


@pytest.mark.asyncio
async def test_idle_three_stage_nudge_then_close(session_factory, seeded, monkeypatch):
    """A2 무음 3단 넛지: in_tr 이 오지 않는 idle 이 지속되면 1단→2단 넛지 주입 후
    3단에서 should_close → 종료 시드 경로 합류로 우아하게 종료(뚝 끊김 없음).

    무음 = in_tr 부재로만 감지. FakeLiveSession 이 첫 턴 후 idle 을 유지하고, 종료 시드
    ([시스템] 통화 시간) 수신 시에만 작별 턴을 방출 → 정상 종료를 서버 무음 경로가 주도.
    """
    # 넛지/종료 타이머를 짧게 — 1단 0.2s, 2단 +0.2s, 3단 +0.2s.
    monkeypatch.setattr(cs, "IDLE_NUDGE1_S", 0.2)
    monkeypatch.setattr(cs, "IDLE_NUDGE2_S", 0.2)
    monkeypatch.setattr(cs, "IDLE_CLOSE_S", 0.2)
    # 5분 시계는 무음보다 훨씬 뒤에 오도록 크게(무음 경로가 먼저 종료를 주도).
    monkeypatch.setattr(cs, "CALL_DURATION_S", 100.0)
    monkeypatch.setattr(cs, "SEED_TO_HANGUP_S", 3.0)

    class IdleForever:
        """첫 턴 후 in_tr 없이 idle 유지 → 종료 시드([시스템] 통화 시간) 수신 시 작별."""

        def __init__(self):
            self.sent_audio: list[bytes] = []
            self.sent_text_turns: list[str] = []
            self._close = asyncio.Event()

        async def send_audio(self, pcm16_16k: bytes) -> None:
            self.sent_audio.append(pcm16_16k)

        async def send_text_turn(self, text: str) -> None:
            self.sent_text_turns.append(text)
            if "통화 시간이 다 됐다" in text:  # 종료 시드(넛지 아님) → 작별 허용
                self._close.set()

        async def events(self):
            yield LiveEvent(kind="out_tr", text="안녕")
            yield LiveEvent(kind="audio", audio=b"\x00\x00")
            yield LiveEvent(kind="turn_end")
            # 이후 idle — in_tr 없음. 무음 워처가 1단/2단 넛지 후 3단에서 should_close.
            await self._close.wait()
            yield LiveEvent(kind="out_tr", text="잘 가요")
            yield LiveEvent(kind="audio", audio=b"\x33\x33")
            yield LiveEvent(kind="turn_end")

    import contextlib as _cl
    holder: dict = {}

    @_cl.asynccontextmanager
    async def factory(client, settings, *, system_instruction, voice):
        s = IdleForever()
        holder["s"] = s
        yield s

    ws = FakeWebSocket(
        [{"type": "websocket.receive",
          "text": json.dumps({"type": "start", "character_id": seeded["character_id"]})}],
        hang=True,
    )

    await run_call(
        ws, app_settings, object(), session_factory,
        member_id=seeded["member_id"], live_session_factory=factory,
    )
    await _wait_analysis_tasks()

    sess = holder["s"]
    # 1단·2단 넛지가 순서대로 주입됐다(in_tr 부재 감지).
    assert any("가볍게 새 화제" in t for t in sess.sent_text_turns), "1단 넛지 미주입"
    assert any("거기 있어" in t for t in sess.sent_text_turns), "2단 넛지 미주입"
    # 3단 → should_close → 종료 시드 주입 → 정상 작별.
    assert any("통화 시간이 다 됐다" in t for t in sess.sent_text_turns), \
        "3단 후 종료 시드 미주입"
    assert b"\x33\x33" in ws.sent_bytes, "작별 오디오 미전달(뚝 끊김)"
    assert any('"call_ended"' in t for t in ws.sent_text), "call_ended 미전송"


@pytest.mark.asyncio
async def test_idle_nudge_reset_on_user_activity(session_factory, seeded, monkeypatch):
    """A2: 넛지 후 학습자 발화(in_tr) 재개 시 silence_stage 가 0 으로 리셋된다."""
    state = cs._CallState()

    class _WS:
        async def send_text(self, text): ...
        async def send_bytes(self, data): ...

    state.turn_id = None
    state.silence_stage = 2
    # in_tr 이벤트가 활동 타임스탬프 갱신 + stage 리셋을 유발.
    await cs._forward_event(_WS(), LiveEvent(kind="in_tr", text="네"), state)
    assert state.silence_stage == 0
    assert state.last_activity_ts is not None


@pytest.mark.asyncio
async def test_inject_nudge_gated_when_busy_or_closing():
    """A2 하드닝(시니어 Q1): _inject_nudge 는 종료중/발화중이면 주입하지 않고 False 를
    돌려준다. 호출부가 이 반환값으로 silence_stage 전진을 게이팅하므로, '단계는 올랐는데
    넛지는 유실'되는 상태-행동 불일치가 원천 차단된다."""

    class _RecordSession:
        def __init__(self):
            self.sent_text_turns: list[str] = []

        async def send_text_turn(self, text: str) -> None:
            self.sent_text_turns.append(text)

    # 발화중(turn_id 있음) → 주입 안 함, False.
    busy = cs._CallState()
    busy.turn_id = "t1"
    sess = _RecordSession()
    assert await cs._inject_nudge(sess, busy, cs._NUDGE_SEED_1) is False
    assert sess.sent_text_turns == []

    # 종료중(should_close) → 주입 안 함, False.
    closing = cs._CallState()
    closing.turn_id = None
    closing.should_close = True
    sess2 = _RecordSession()
    assert await cs._inject_nudge(sess2, closing, cs._NUDGE_SEED_1) is False
    assert sess2.sent_text_turns == []

    # idle & 정상 → 주입, True.
    idle = cs._CallState()
    idle.turn_id = None
    sess3 = _RecordSession()
    assert await cs._inject_nudge(sess3, idle, cs._NUDGE_SEED_1) is True
    assert sess3.sent_text_turns == [cs._NUDGE_SEED_1]


def test_resolve_call_duration_clamps_and_defaults():
    """통화 길이 override(데모/dev): 3~15분 클램프, prod 무시(기본값), None 기본값."""
    from types import SimpleNamespace
    dev = SimpleNamespace(ENV="dev")
    prod = SimpleNamespace(ENV="prod")
    base = cs.CALL_DURATION_S
    assert cs._resolve_call_duration(dev, None) == base       # 미지정 → 기본
    assert cs._resolve_call_duration(dev, 3) == 180.0         # 하한
    assert cs._resolve_call_duration(dev, 15) == 900.0        # 상한
    assert cs._resolve_call_duration(dev, 1) == 180.0         # < 3 → 3분 클램프
    assert cs._resolve_call_duration(dev, 99) == 900.0        # > 15 → 15분 클램프
    assert cs._resolve_call_duration(prod, 10) == base        # prod → override 무시


class _RegroundFake:
    """on_user_turn 재접지 검증용 가짜 세션 — send_reground(turn_complete) 기록."""

    def __init__(self, script):
        self._script = script  # events() 가 yield 할 LiveEvent 리스트 or 콜백
        self.sent_audio: list[bytes] = []
        self.sent_text_turns: list[str] = []
        self.regrounds: list[tuple[str, bool]] = []  # (text, turn_complete)
        self.injected = asyncio.Event()

    async def send_audio(self, pcm16_16k: bytes) -> None:
        self.sent_audio.append(pcm16_16k)

    async def send_text_turn(self, text: str) -> None:
        self.sent_text_turns.append(text)

    async def send_reground(self, text: str, *, turn_complete: bool = True) -> None:
        self.regrounds.append((text, turn_complete))
        self.injected.set()

    async def events(self):
        for item in self._script:
            if callable(item):
                await item(self)
            else:
                yield item


async def _run_with_fake(fake, session_factory, seeded):
    import contextlib as _cl
    holder = {"s": fake}

    @_cl.asynccontextmanager
    async def factory(client, settings, *, system_instruction, voice):
        yield fake

    ws = FakeWebSocket(
        [{"type": "websocket.receive",
          "text": json.dumps({"type": "start", "character_id": seeded["character_id"]})}],
        hang=True,
    )
    await run_call(ws, app_settings, object(), session_factory,
                   member_id=seeded["member_id"], live_session_factory=factory)
    await _wait_analysis_tasks()
    return ws


@pytest.mark.asyncio
async def test_reground_on_user_turn_attaches_once(session_factory, seeded, monkeypatch):
    """on_user_turn: arm 후 유저 발화 시작(첫 in_tr)에 리마인더가 turn_complete=False 로 딱 1회
    얹힌다. 같은 턴 두 번째 in_tr 은 재주입 없음. send_text_turn(종료/무음)와 분리."""
    monkeypatch.setattr(cs, "REGROUND_MODE", "on_user_turn")
    monkeypatch.setattr(cs, "REGROUND_ATTACH_AT", "first")
    monkeypatch.setattr(cs, "REGROUND_AT_FRACTION", 0.001)  # fire_at ≈ 통화 시작 직후

    async def wait_arm(f):
        await asyncio.sleep(0.5)  # _reground_once 가 arm(reground_pending) 할 시간
    fake = _RegroundFake([
        LiveEvent(kind="out_tr", text="안녕"),   # 비버 오프닝 → call_start_ts 세팅
        LiveEvent(kind="turn_end"),
        wait_arm,
        LiveEvent(kind="in_tr", text="네", is_final=False),      # 첫 in_tr → 얹기
        LiveEvent(kind="in_tr", text=" 좋아요", is_final=True),   # 같은 턴 → 재주입 없음
        lambda f: f.injected.wait(),
    ])
    await _run_with_fake(fake, session_factory, seeded)

    assert len(fake.regrounds) == 1, "재접지 얹기가 정확히 1회가 아님"
    text, tc = fake.regrounds[0]
    assert tc is False, "on_user_turn 은 turn_complete=False 여야 함(유저 턴에 얹기)"
    assert "캐릭터답게 한마디" in text, "재접지 문구가 행동 지시 리마인더가 아님"
    assert not any(text == t for t in fake.sent_text_turns), "재접지가 send_text_turn 로 샜다"


@pytest.mark.asyncio
async def test_reground_skipped_near_close(session_factory, seeded, monkeypatch):
    """핵심 안전(가드①): 종료 시드(close_seed_sent) 이후 늦은 유저 in_tr 에는 재접지를 얹지
    않는다 — 작별 턴 오염(174/178 재발) 방지."""
    monkeypatch.setattr(cs, "REGROUND_MODE", "on_user_turn")
    monkeypatch.setattr(cs, "REGROUND_AT_FRACTION", 0.001)
    monkeypatch.setattr(cs, "CALL_DURATION_S", 0.3)   # 곧 종료(_watch_call_clock)
    monkeypatch.setattr(cs, "SEED_TO_HANGUP_S", 3.0)
    close_seen = asyncio.Event()

    class Fake(_RegroundFake):
        async def send_text_turn(self, text):
            await _RegroundFake.send_text_turn(self, text)
            if "통화 시간이 다 됐다" in text:  # 종료 시드 주입됨
                close_seen.set()

        async def events(self):
            yield LiveEvent(kind="out_tr", text="안녕")   # call_start_ts 세팅
            yield LiveEvent(kind="turn_end")
            await close_seen.wait()                        # should_close + close_seed_sent 이후
            yield LiveEvent(kind="in_tr", text="어 나 갈게")  # 늦은 유저 발화 → 재접지 금지
            yield LiveEvent(kind="out_tr", text="Bye!")    # 작별
            yield LiveEvent(kind="turn_end")

    fake = Fake([])  # script 미사용(events override)
    await _run_with_fake(fake, session_factory, seeded)
    assert fake.regrounds == [], "종료 구간에서 재접지가 얹혔다(가드① 위반 — 작별 오염 위험)"


@pytest.mark.asyncio
async def test_go_away_triggers_graceful_close(session_factory, seeded, monkeypatch):
    """A3 GoAway: events() 가 go_away 이벤트를 내면 idle 에서 즉시 종료 시드 주입 →
    정상 작별 종료(연결 뚝 끊기 전 선제 마무리)."""
    monkeypatch.setattr(cs, "SEED_TO_HANGUP_S", 3.0)

    class GoAwayThenBye:
        def __init__(self):
            self.sent_audio: list[bytes] = []
            self.sent_text_turns: list[str] = []
            self._close = asyncio.Event()

        async def send_audio(self, pcm16_16k: bytes) -> None:
            self.sent_audio.append(pcm16_16k)

        async def send_text_turn(self, text: str) -> None:
            self.sent_text_turns.append(text)
            if text.startswith("[시스템]"):
                self._close.set()

        async def events(self):
            yield LiveEvent(kind="out_tr", text="안녕")
            yield LiveEvent(kind="turn_end")   # idle 로 전환(turn_id None)
            yield LiveEvent(kind="go_away", time_left="10s")
            await self._close.wait()           # 종료 시드 주입 대기
            yield LiveEvent(kind="out_tr", text="잘 가요")
            yield LiveEvent(kind="audio", audio=b"\x44\x44")
            yield LiveEvent(kind="turn_end")

    import contextlib as _cl
    holder: dict = {}

    @_cl.asynccontextmanager
    async def factory(client, settings, *, system_instruction, voice):
        s = GoAwayThenBye()
        holder["s"] = s
        yield s

    ws = FakeWebSocket(
        [{"type": "websocket.receive",
          "text": json.dumps({"type": "start", "character_id": seeded["character_id"]})}],
        hang=True,
    )

    await run_call(
        ws, app_settings, object(), session_factory,
        member_id=seeded["member_id"], live_session_factory=factory,
    )
    await _wait_analysis_tasks()

    sess = holder["s"]
    assert any(t.startswith("[시스템]") for t in sess.sent_text_turns), \
        "GoAway 후 종료 시드 미주입"
    assert b"\x44\x44" in ws.sent_bytes, "작별 오디오 미전달"
    assert any('"call_ended"' in t for t in ws.sent_text), "call_ended 미전송"


@pytest.mark.asyncio
async def test_go_away_event_normalized(monkeypatch):
    """A3: core.gemini_live.events() 가 response.go_away 를 LiveEvent(kind=go_away)로
    정규화하고 time_left 를 담는다(SDK 필드는 getattr 방어)."""
    from core.gemini_live import GeminiLiveSession

    class _GoAway:
        time_left = "12s"

    class _Resp:
        data = None
        server_content = None
        go_away = _GoAway()

    class _Raw:
        def __init__(self):
            self._sent = 0

        def receive(self):
            # receive() 는 매 턴 async iterator 를 반환(coroutine 아님).
            # 첫 턴엔 go_away 1건, 다음 턴엔 0건(수신종료 → 루프 종료).
            first = self._sent == 0
            self._sent = 1

            async def _gen():
                if first:
                    yield _Resp()

            return _gen()

    sess = GeminiLiveSession(_Raw())
    kinds = []
    async for ev in sess.events():
        kinds.append((ev.kind, ev.time_left))
    assert ("go_away", "12s") in kinds


@pytest.mark.asyncio
async def test_run_call_disconnect_before_start_is_graceful(session_factory, seeded):
    """start 수신 전 클라가 끊으면 통화 생성 없이 조용히 종료(no Call 행)."""
    holder: dict = {}
    ws = FakeWebSocket([{"type": "websocket.disconnect"}])
    await run_call(
        ws, app_settings, object(), session_factory,
        member_id=seeded["member_id"],
        live_session_factory=make_live_factory(holder),
    )
    db = session_factory()
    try:
        assert db.query(Call).count() == 0
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# (c) analyze_call 단독
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_analyze_call_creates_sentence_and_done(session_factory, seeded):
    # 통화 + 전사 행 시드
    db = session_factory()
    try:
        call = Call(member_id=seeded["member_id"],
                    character_id=seeded["character_id"], status="analyzing")
        db.add(call)
        db.flush()
        db.add(CallRawData(call_id=call.call_id, role="user", turn_index=0,
                           content="안녕"))
        db.add(CallRawData(call_id=call.call_id, role="beaver", turn_index=1,
                           content="안녕하세요"))
        db.commit()
        call_id = call.call_id
    finally:
        db.close()

    await svc.analyze_call(call_id, object(), app_settings, session_factory, locale="en")

    db = session_factory()
    try:
        call = db.get(Call, call_id)
        assert call.status == "done"
        assert call.summary == "짧은 통화 요약"
        assert call.mode == "chat"
        sents = db.query(Sentence).filter(Sentence.call_id == call_id).all()
        assert len(sents) == 1
        assert sents[0].korean_sentence == "안녕하세요"
        assert sents[0].source_type == "asked"
        # placeholder Evaluation 도 함께 생성
        assert sents[0].evaluation is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_analyze_call_empty_dialog_done_no_sentence(session_factory, seeded):
    """전사가 없으면 LLM 호출 없이 done(빈 결과)."""
    db = session_factory()
    try:
        call = Call(member_id=seeded["member_id"],
                    character_id=seeded["character_id"], status="analyzing")
        db.add(call)
        db.commit()
        call_id = call.call_id
    finally:
        db.close()

    await svc.analyze_call(call_id, object(), app_settings, session_factory, locale="en")

    db = session_factory()
    try:
        assert db.get(Call, call_id).status == "done"
        assert db.query(Sentence).filter(Sentence.call_id == call_id).count() == 0
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# (c2) P2.6 — 결과 페이지 체감 속도: done 선커밋 + 후행 단계 실패 격리
# --------------------------------------------------------------------------- #
def _seed_call_with_dialog(session_factory, seeded) -> int:
    """analyzing 통화 + 전사 2행(user/beaver) 시드 — analyze_call 입력 공용."""
    db = session_factory()
    try:
        call = Call(member_id=seeded["member_id"],
                    character_id=seeded["character_id"], status="analyzing")
        db.add(call)
        db.flush()
        db.add(CallRawData(call_id=call.call_id, role="user", turn_index=0,
                           content="안녕"))
        db.add(CallRawData(call_id=call.call_id, role="beaver", turn_index=1,
                           content="안녕하세요"))
        db.commit()
        return call.call_id
    finally:
        db.close()


@pytest.mark.asyncio
async def test_status_done_before_tts_completes(session_factory, seeded, monkeypatch):
    """P2.6 (a): TTS 가 느리거나 실패해도 _save_analysis 커밋 직후 status=done +
    Sentence 존재 — 결과 페이지가 TTS×N 을 기다리지 않는다."""
    call_id = _seed_call_with_dialog(session_factory, seeded)

    tts_started = asyncio.Event()
    tts_release = asyncio.Event()

    async def _slow_failing_tts(*_a, **_k):  # 느림(게이트) + 최종 실패까지 겸검증
        tts_started.set()
        await tts_release.wait()
        raise RuntimeError("tts down")

    monkeypatch.setattr(svc.tts, "synthesize", _slow_failing_tts)

    task = asyncio.create_task(
        svc.analyze_call(call_id, object(), app_settings, session_factory, locale="en")
    )
    await asyncio.wait_for(tts_started.wait(), timeout=5.0)

    # TTS 진행중(미완) 시점 — 이미 done 커밋 + Sentence 사용 가능(voice_url 만 미정).
    db = session_factory()
    try:
        assert db.get(Call, call_id).status == "done", "TTS 완료 전에 done 이어야 함(P2.6)"
        sents = db.query(Sentence).filter(Sentence.call_id == call_id).all()
        assert len(sents) == 1
        assert sents[0].voice_url is None  # 온디맨드 합성 폴백 대상
    finally:
        db.close()

    tts_release.set()
    await asyncio.wait_for(task, timeout=5.0)

    # TTS 실패(done 이후)는 status 를 되돌리지 않는다.
    db = session_factory()
    try:
        assert db.get(Call, call_id).status == "done"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_post_done_mastery_failure_keeps_done(session_factory, seeded,
                                                    monkeypatch, caplog):
    """P2.6 (b): done 이후 체크판 단계 예외 → status=done 유지 + 경고 로그(무손상)."""
    import logging as _logging

    call_id = _seed_call_with_dialog(session_factory, seeded)

    def _boom(*_a, **_k):
        raise RuntimeError("mastery boom")

    monkeypatch.setattr(svc, "_apply_call_mastery", _boom)

    with caplog.at_level(_logging.WARNING):
        await svc.analyze_call(
            call_id, object(), app_settings, session_factory,
            locale="en", member_id=seeded["member_id"],
        )

    db = session_factory()
    try:
        call = db.get(Call, call_id)
        assert call.status == "done", "done 이후 체크판 실패가 status 를 되돌림(P2.6 위반)"
        assert call.summary == "짧은 통화 요약"  # 결과 화면 데이터 무손상
    finally:
        db.close()
    assert any("체크판" in r.getMessage() for r in caplog.records), "체크판 실패 로그 부재"


def test_save_segments_deferred_audio_then_upload(session_factory, seeded):
    """P2.6 세그먼트 분리: upload_audio=False 는 텍스트 행만 커밋(voice_url None) +
    pending 반환 → upload_segment_audio 실행 시 voice_url 이 채워진다."""
    db = session_factory()
    try:
        call = Call(member_id=seeded["member_id"],
                    character_id=seeded["character_id"], status="ongoing")
        db.add(call)
        db.commit()
        call_id = call.call_id
    finally:
        db.close()

    segs = [
        {"turn_index": 0, "role": "user", "text": "안녕", "pcm": b"\x00\x00"},
        {"turn_index": 1, "role": "beaver", "text": "안녕하세요", "pcm": b""},
    ]
    db = session_factory()
    try:
        pending = svc.save_segments(db, call_id, segs, seeded["member_id"],
                                    upload_audio=False)
    finally:
        db.close()

    # 텍스트 행은 즉시 커밋, 오디오는 전부 미업로드(voice_url None).
    db = session_factory()
    try:
        rows = db.query(CallRawData).order_by(CallRawData.turn_index).all()
        assert len(rows) == 2
        assert rows[0].content == "안녕"
        assert all(r.voice_url is None for r in rows)
    finally:
        db.close()
    # pending 은 pcm 있는 행만(빈 pcm 제외).
    assert len(pending) == 1
    assert pending[0]["role"] == "user" and pending[0]["turn_index"] == 0
    assert pending[0]["call_raw_data_id"] == rows[0].call_raw_data_id

    # 후행 업로드 → 해당 행 voice_url 만 채워짐(storage 스텁 key).
    db = session_factory()
    try:
        n = svc.upload_segment_audio(db, call_id, seeded["member_id"], pending)
        assert n == 1
    finally:
        db.close()

    db = session_factory()
    try:
        rows = db.query(CallRawData).order_by(CallRawData.turn_index).all()
        assert rows[0].voice_url == "stub-key"
        assert rows[1].voice_url is None  # pcm 없던 행은 그대로
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# (d) get_status 소유자 가드 + load_call_setup / save_segments 단위
# --------------------------------------------------------------------------- #
def test_get_status_owner_guard(session_factory, seeded):
    db = session_factory()
    try:
        call = Call(member_id=seeded["member_id"],
                    character_id=seeded["character_id"], status="ongoing")
        db.add(call)
        db.commit()
        call_id = call.call_id
        # 소유자: 상태 반환
        assert svc.get_status(db, call_id, seeded["member_id"]) == "ongoing"
        # 타 회원: None
        assert svc.get_status(db, call_id, seeded["member_id"] + 999) is None
        # 없는 통화: None
        assert svc.get_status(db, 999999, seeded["member_id"]) is None
    finally:
        db.close()


def test_status_endpoint_unknown_for_other_member(session_factory, seeded):
    """GET /calls/{id}/status — 타인 통화면 'unknown'."""
    from fastapi.testclient import TestClient

    db = session_factory()
    try:
        call = Call(member_id=seeded["member_id"],
                    character_id=seeded["character_id"], status="done")
        db.add(call)
        # 타 회원도 한 명 생성(다른 auth uuid)
        other = Member(language="en", korean_level=1, onboarding_completed=True,
                       auth_user_id="auth-other")
        db.add(other)
        db.commit()
        call_id = call.call_id
    finally:
        db.close()

    app = _build_app(session_factory)
    client = TestClient(app)

    r1 = client.get(f"/api/v1/calls/{call_id}/status",
                    headers={"Authorization": "Bearer auth-member"})
    assert r1.status_code == 200
    assert r1.json()["status"] == "done"

    r2 = client.get(f"/api/v1/calls/{call_id}/status",
                    headers={"Authorization": "Bearer auth-other"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "unknown"


def test_load_call_setup_returns_plain_values(session_factory, seeded):
    db = session_factory()
    try:
        setup = svc.load_call_setup(db, seeded["member_id"], seeded["character_id"])
        assert setup["locale"] == "en"
        assert setup["voice"] == "Fenrir"
        assert setup["level_profile"] == "초급 학습자"
        assert "여행" in setup["interests"]
        # ORM 객체가 아니라 평범한 값
        assert isinstance(setup, dict)
    finally:
        db.close()


def test_save_segments_writes_rows_with_voice_url(session_factory, seeded):
    db = session_factory()
    try:
        call = Call(member_id=seeded["member_id"],
                    character_id=seeded["character_id"], status="ongoing")
        db.add(call)
        db.commit()
        call_id = call.call_id
    finally:
        db.close()

    db = session_factory()
    try:
        segs = [
            {"turn_index": 0, "role": "user", "text": "안녕", "pcm": b"\x00\x00"},
            {"turn_index": 1, "role": "beaver", "text": "안녕하세요", "pcm": b""},
        ]
        n = svc.save_segments(db, call_id, segs, seeded["member_id"])
        assert n == 2
    finally:
        db.close()

    db = session_factory()
    try:
        rows = db.query(CallRawData).order_by(CallRawData.turn_index).all()
        assert len(rows) == 2
        assert rows[0].voice_url == "stub-key"  # pcm 있으면 업로드 key
        assert rows[1].voice_url is None         # pcm 없으면 None(전사만)
        assert rows[0].content == "안녕"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# (e) 레벨테스트(Phase 1 — 비버 자율 진행 · 서버 무주입)
#
# 사다리·판정 사이드카·주입 기계·_watch_tree·tree 필드는 전부 삭제됐다. 서버는 통화
# 중 질문을 주입하지 않는다 — 비버가 system_instruction 만으로 자유 진행하고, 통화 중
# 서버가 세션에 넣는 텍스트 턴은 선톡 시드 + 무음 넛지 + 종료 시드 3곳뿐이다.
# 종료는 3분 하드캡(LEVELTEST_MAX_S) 또는 무음 3단(25/8/10) → CLOSE_SEED_LEVELTEST.
#
# 공용 러너 _run_call_with 는 call_type override 로 level_test / normal 을 같은 엔진으로
# 구동하고, factory 가 받은 kwargs(=tools 전달 여부)를 holder 에 기록한다. 판정 사이드카가
# 없으므로 judge monkeypatch 는 불필요하다.
# --------------------------------------------------------------------------- #
def _start_incoming(seeded, call_type=None):
    payload = {"type": "start", "character_id": seeded["character_id"]}
    if call_type is not None:
        payload["call_type"] = call_type
    return {"type": "websocket.receive", "text": json.dumps(payload)}


async def _run_call_with(session, seeded, session_factory, *, call_type=None):
    """가짜 세션 하나로 run_call 을 끝까지 돌리고 (ws, holder) 반환.

    holder["kwargs"] 에는 factory 가 받은 키워드(=tools 전달 여부)가 담긴다.
    클라는 start 만 보내고 침묵(hang=True) — 종료는 서버(캡/신호/무음)가 주도.
    """
    import contextlib as _cl

    holder: dict = {"s": session}

    @_cl.asynccontextmanager
    async def factory(client, settings, **kwargs):
        holder["kwargs"] = kwargs
        holder["system_instruction"] = kwargs.get("system_instruction")
        yield session

    ws = FakeWebSocket([_start_incoming(seeded, call_type)], hang=True)
    await run_call(
        ws, app_settings, object(), session_factory,
        member_id=seeded["member_id"], live_session_factory=factory,
    )
    await _wait_analysis_tasks()
    return ws, holder


from core.persona_prompt import CLOSE_SEED_LEVELTEST


# --- 부트스트랩 + 무주입: 오프닝은 '[통화 시작]' 선톡 시드, 서버 질문 주입 0 ------- #
@pytest.mark.asyncio
async def test_leveltest_opening_seed_bootstraps_without_injection(session_factory, seeded):
    """Phase 1: 레벨테스트 선톡 시드(sent_text_turns[0])는 '[통화 시작]' 오프닝이고,
    서버는 통화 중 질문을 주입하지 않는다(sent_text_turns 에 '[다음]' 0건).
    비버가 첫 질문을 system_instruction 만으로 스스로 시작한다(사다리 부트스트랩 없음)."""
    sess = FakeLiveSession()  # 오프닝 한 턴 후 스트림 종료(자연 종료)
    ws, holder = await _run_call_with(sess, seeded, session_factory, call_type="level_test")

    turns = holder["s"].sent_text_turns
    assert turns and turns[0].startswith("[통화 시작]"), "레벨테스트 오프닝 선톡 시드가 아님"
    assert "가벼운 인사 한 마디 + 바로 질문" in turns[0], "레벨테스트 오프닝 문구 아님"
    assert not any(t.startswith("[다음]") for t in turns), \
        "서버가 질문을 주입했다('[다음]' 시드 — 무주입 위반)"


# --- 무주입: 유저가 여러 번 답해도 서버는 '[다음]' 질문을 0건 주입한다 ----------- #
@pytest.mark.asyncio
async def test_leveltest_no_injection_across_multiple_answers(session_factory, seeded):
    """Phase 1 AC(무주입·이중발화 방지): 가짜 세션이 in_tr(유저 답)을 여러 턴 방출해도
    서버는 '[다음]' 질문을 0건 주입한다. 통화 중 서버가 세션에 넣는 텍스트 턴은 질문
    마커('[다음]')가 없는 선톡 시드([통화 시작])뿐이다(자연 종료 — 넛지·종료 시드 미발동)."""

    class MultiAnswer(FakeLiveSession):
        async def events(self):
            yield LiveEvent(kind="out_tr", text="이름이 뭐예요?")
            yield LiveEvent(kind="audio", audio=b"\x00\x00")
            yield LiveEvent(kind="turn_end")
            for i in range(4):
                yield LiveEvent(kind="in_tr", text=f"저는 학생이에요 {i}", is_final=True)
                yield LiveEvent(kind="out_tr", text="좋아요, 사는 곳은요?")
                yield LiveEvent(kind="audio", audio=b"\x00\x00")
                yield LiveEvent(kind="turn_end")
            # 스트림 종료 → 자연 종료(_CallFinished). 서버가 주입한 텍스트는 선톡 시드뿐.

    sess = MultiAnswer()
    ws, holder = await _run_call_with(sess, seeded, session_factory, call_type="level_test")

    turns = holder["s"].sent_text_turns
    assert not any(t.startswith("[다음]") for t in turns), \
        "유저가 여러 번 답했는데 서버가 질문을 주입했다(무주입 위반)"
    # 마커 미노출 근사: 서버가 주입하는 문자열엔 질문 마커가 없다(선톡 시드만).
    assert turns == [t for t in turns if t.startswith("[통화 시작]")], \
        "통화 중 서버 텍스트 턴이 선톡 시드가 아님(주입 누수)"


# --- 3분캡: 유저 무응답/자유대화에도 캡이 종료를 몬다(종료 시드=CLOSE_SEED_LEVELTEST) - #
@pytest.mark.asyncio
async def test_leveltest_no_answer_closes_at_cap(session_factory, seeded, monkeypatch):
    """Phase 1 3분캡: 유저 무응답이어도 레벨테스트 하드캡(LEVELTEST_MAX_S)이 시계로
    종료를 몬다 — 종료 시드(CLOSE_SEED_LEVELTEST: '오늘 대화는 여기까지')·작별·call_ended.
    서버는 통화 중 질문을 주입하지 않는다('[다음]' 0건)."""
    monkeypatch.setattr(cs, "LEVELTEST_MAX_S", 0.3)   # 3분 캡 → 0.3s 로 축소
    monkeypatch.setattr(cs, "SEED_TO_HANGUP_S", 3.0)

    class IdleUntilCap:
        def __init__(self):
            self.sent_audio: list[bytes] = []
            self.sent_text_turns: list[str] = []
            self._close = asyncio.Event()

        async def send_audio(self, pcm16_16k: bytes) -> None:
            self.sent_audio.append(pcm16_16k)

        async def send_text_turn(self, text: str) -> None:
            self.sent_text_turns.append(text)
            if "오늘 대화는 여기까지" in text:
                self._close.set()

        async def events(self):
            yield LiveEvent(kind="out_tr", text="안녕하세요")
            yield LiveEvent(kind="audio", audio=b"\x00\x00")
            yield LiveEvent(kind="turn_end")
            await self._close.wait()               # 캡이 종료 시드 주입할 때까지 idle
            yield LiveEvent(kind="out_tr", text="결과는 곧 알려줄게요")
            yield LiveEvent(kind="audio", audio=b"\x77\x77")
            yield LiveEvent(kind="turn_end")

    sess = IdleUntilCap()
    ws, _ = await _run_call_with(sess, seeded, session_factory, call_type="level_test")

    assert not any(t.startswith("[다음]") for t in sess.sent_text_turns), \
        "서버가 질문을 주입했다(무주입 위반)"
    assert any("오늘 대화는 여기까지" in t for t in sess.sent_text_turns), \
        "캡 종료 시드(CLOSE_SEED_LEVELTEST) 미주입"
    assert b"\x77\x77" in ws.sent_bytes, "작별 오디오 미전달(뚝 끊김)"
    assert any('"call_ended"' in t for t in ws.sent_text), "call_ended 미전송"


# --- 무음 캐던스 단축 + 새 종료 시드 문구 ------------------------------------- #
@pytest.mark.asyncio
async def test_leveltest_idle_cadence_uses_shortened_seeds(session_factory, seeded, monkeypatch):
    """T3(갱신): 레벨테스트 무음 3단은 단축 캐던스(25/8/10 → 축소) + 레벨테스트 1단 시드
    (순화된 신 문구 '방금 한 질문을 더 쉽게 바꾸거나 선택지를')를 쓴다. 일반 1단 시드는
    나오면 안 되고, 3단 종료 시드는 CLOSE_SEED_LEVELTEST('오늘 대화는 여기까지')다."""
    monkeypatch.setattr(cs, "LEVELTEST_IDLE_NUDGE1_S", 0.2)
    monkeypatch.setattr(cs, "LEVELTEST_IDLE_NUDGE2_S", 0.2)
    monkeypatch.setattr(cs, "LEVELTEST_IDLE_CLOSE_S", 0.2)
    monkeypatch.setattr(cs, "LEVELTEST_MAX_S", 100.0)  # 캡은 무음 경로보다 뒤(무음이 종료 주도)
    monkeypatch.setattr(cs, "SEED_TO_HANGUP_S", 3.0)

    class LevelIdleForever:
        def __init__(self):
            self.sent_audio: list[bytes] = []
            self.sent_text_turns: list[str] = []
            self._close = asyncio.Event()

        async def send_audio(self, pcm16_16k: bytes) -> None:
            self.sent_audio.append(pcm16_16k)

        async def send_text_turn(self, text: str) -> None:
            self.sent_text_turns.append(text)
            if "오늘 대화는 여기까지" in text:  # 레벨테스트 종료 시드(넛지 아님) → 작별
                self._close.set()

        async def events(self):
            yield LiveEvent(kind="out_tr", text="안녕하세요")
            yield LiveEvent(kind="audio", audio=b"\x00\x00")
            yield LiveEvent(kind="turn_end")
            await self._close.wait()               # 3단 → 종료 시드 후 작별
            yield LiveEvent(kind="out_tr", text="결과는 곧")
            yield LiveEvent(kind="audio", audio=b"\x88\x88")
            yield LiveEvent(kind="turn_end")

    sess = LevelIdleForever()
    ws, _ = await _run_call_with(sess, seeded, session_factory, call_type="level_test")

    # 1단: 레벨테스트 전용 시드(순화된 신 문구 — 방금 질문을 더 쉽게/선택지로 다시).
    assert any("방금 한 질문을 더 쉽게 바꾸거나 선택지를" in t for t in sess.sent_text_turns), \
        "레벨테스트 1단 넛지 미주입(신 문구 아님)"
    # 실제 상수와 동치인지 확인(테스트-구현 문구 드리프트 방지).
    assert any(t == cs._NUDGE_SEED_1_LEVELTEST for t in sess.sent_text_turns), \
        "1단 넛지가 _NUDGE_SEED_1_LEVELTEST 상수와 불일치"
    # 일반 통화 1단 시드가 새면 안 된다(회귀).
    assert not any("가볍게 새 화제로 한 문장만" in t for t in sess.sent_text_turns), \
        "레벨테스트에 일반 1단 넛지가 샜다"
    # 2단: 공통 확인 넛지.
    assert any("거기 있어" in t for t in sess.sent_text_turns), "2단 확인 넛지 미주입"
    # 3단: 레벨테스트 종료 시드 → 작별(새 문구).
    assert any("오늘 대화는 여기까지" in t for t in sess.sent_text_turns), "3단 종료 시드 미주입"
    assert b"\x88\x88" in ws.sent_bytes, "작별 오디오 미전달"
    assert any('"call_ended"' in t for t in ws.sent_text), "call_ended 미전송"


# --- tools 분기(갱신): 레벨테스트도 tools 미전달(tools=None) + 레벨테스트만 tree 활성 --- #
@pytest.mark.asyncio
async def test_tools_not_passed_for_either_call_type(session_factory, seeded, monkeypatch):
    """Phase 1: 레벨테스트도 in-band tool 을 안 쓴다 — 두 콜타입 모두 factory 에 tools 키가
    흐르지 않는다(하위호환 시그니처 유지)."""
    lt_sess = FakeLiveSession()
    _, lt_holder = await _run_call_with(
        lt_sess, seeded, session_factory, call_type="level_test"
    )
    assert "tools" not in lt_holder["kwargs"], "레벨테스트 factory 에 tools 키가 흘렀다(사이드카 설계 위반)"
    assert "system_instruction" in lt_holder["kwargs"] and "voice" in lt_holder["kwargs"]

    n_sess = FakeLiveSession()
    _, n_holder = await _run_call_with(
        n_sess, seeded, session_factory, call_type="normal"
    )
    assert "tools" not in n_holder["kwargs"], "일반 통화 factory 에 tools 키가 흘렀다(회귀)"
    assert "system_instruction" in n_holder["kwargs"] and "voice" in n_holder["kwargs"]


# --- 일반 통화 무영향: tree=None 경로에서 판정/주입 0 --------------------------- #
@pytest.mark.asyncio
async def test_normal_call_no_ladder_activity(session_factory, seeded, monkeypatch):
    """일반 통화는 레벨테스트 경로와 무관 — 서버가 '[다음]' 질문을 주입하지 않고,
    일반 종료 시드('통화 시간이 다 됐다')로 정상 종료(레벨테스트 종료 시드 누수 없음)."""
    monkeypatch.setattr(cs, "CALL_DURATION_S", 0.3)   # 5분 → 0.3s
    monkeypatch.setattr(cs, "SEED_TO_HANGUP_S", 3.0)
    monkeypatch.setattr(cs, "REGROUND_MODE", "off")   # 재접지 격리(종료만 검증)

    class NormalIdleClose:
        def __init__(self):
            self.sent_audio: list[bytes] = []
            self.sent_text_turns: list[str] = []
            self._close = asyncio.Event()

        async def send_audio(self, pcm16_16k: bytes) -> None:
            self.sent_audio.append(pcm16_16k)

        async def send_text_turn(self, text: str) -> None:
            self.sent_text_turns.append(text)
            if text.startswith("[시스템]"):
                self._close.set()

        async def events(self):
            yield LiveEvent(kind="out_tr", text="안녕")
            yield LiveEvent(kind="audio", audio=b"\x00\x00")
            yield LiveEvent(kind="turn_end")
            # 유저가 말해도 일반 통화는 판정 사이드카를 발사하지 않는다(tree=None).
            yield LiveEvent(kind="in_tr", text="저는 서울에 살아요", is_final=True)
            yield LiveEvent(kind="out_tr", text="그렇군요")
            yield LiveEvent(kind="audio", audio=b"\x11\x11")
            yield LiveEvent(kind="turn_end")
            await self._close.wait()
            yield LiveEvent(kind="out_tr", text="잘 가요")
            yield LiveEvent(kind="audio", audio=b"\x99\x99")
            yield LiveEvent(kind="turn_end")

    sess = NormalIdleClose()
    ws, _ = await _run_call_with(sess, seeded, session_factory, call_type="normal")

    assert not any(t.startswith("[다음]") for t in sess.sent_text_turns), \
        "일반 통화에 사다리 질문('[다음]')이 주입됨(회귀)"
    # 일반 종료 시드 사용 — 레벨테스트 시드 아님.
    assert any("통화 시간이 다 됐다" in t for t in sess.sent_text_turns), "일반 종료 시드 미주입"
    assert not any("오늘 대화는 여기까지" in t for t in sess.sent_text_turns), \
        "일반 통화에 레벨테스트 종료 시드가 샜다"
    assert b"\x99\x99" in ws.sent_bytes, "작별 오디오 미전달"
    assert any('"call_ended"' in t for t in ws.sent_text), "call_ended 미전송"


@pytest.mark.asyncio
async def test_normal_call_unaffected_by_tool_use(session_factory, seeded, monkeypatch):
    """T-회귀: 일반 통화는 tool-use 무관 — send_tool_response 미호출, 일반 종료 시드
    ('통화 시간이 다 됐다') + 정상 작별. 레벨테스트 시드는 나오면 안 된다."""
    monkeypatch.setattr(cs, "CALL_DURATION_S", 0.3)   # 5분 → 0.3s
    monkeypatch.setattr(cs, "SEED_TO_HANGUP_S", 3.0)
    monkeypatch.setattr(cs, "REGROUND_MODE", "off")   # 재접지 격리(종료만 검증)

    class NormalIdleClose:
        def __init__(self):
            self.sent_audio: list[bytes] = []
            self.sent_text_turns: list[str] = []
            self.tool_acks: list[tuple] = []
            self._close = asyncio.Event()

        async def send_audio(self, pcm16_16k: bytes) -> None:
            self.sent_audio.append(pcm16_16k)

        async def send_text_turn(self, text: str) -> None:
            self.sent_text_turns.append(text)
            if text.startswith("[시스템]"):
                self._close.set()

        async def send_tool_response(self, fn_id, fn_name) -> None:
            self.tool_acks.append((fn_id, fn_name))

        async def events(self):
            yield LiveEvent(kind="out_tr", text="안녕")
            yield LiveEvent(kind="audio", audio=b"\x00\x00")
            yield LiveEvent(kind="turn_end")
            await self._close.wait()
            yield LiveEvent(kind="out_tr", text="잘 가요")
            yield LiveEvent(kind="audio", audio=b"\x99\x99")
            yield LiveEvent(kind="turn_end")

    sess = NormalIdleClose()
    ws, _ = await _run_call_with(sess, seeded, session_factory, call_type="normal")

    assert sess.tool_acks == [], "일반 통화에서 tool ack 발생(회귀)"
    # 일반 종료 시드 사용 — 레벨테스트 시드 아님.
    assert any("통화 시간이 다 됐다" in t for t in sess.sent_text_turns), "일반 종료 시드 미주입"
    assert not any("실력 파악이 끝났다" in t for t in sess.sent_text_turns), \
        "일반 통화에 레벨테스트 종료 시드가 샜다"
    assert b"\x99\x99" in ws.sent_bytes, "작별 오디오 미전달"
    assert any('"call_ended"' in t for t in ws.sent_text), "call_ended 미전송"


# --------------------------------------------------------------------------- #
# (f) 레벨테스트 Phase 2 — 조용한 밴드 관측 + 조기종료 (질문 주입 0)
#
# 유저 답변마다 사이드카 svc.classify_leveltest_band(answer_text, prior_question) 가
# 밴드(0 survival~3 advanced, None=판정불가)를 조용히 관측한다. 서버는 obs_max/plateau
# 를 추적해 천장(_band_ceiling_reached)에 닿으면 종료 시드(CLOSE_SEED_LEVELTEST)만
# 주입한다 — ★ 질문 주입 없음('[다음]' 0건). 백스톱은 3분캡·무음3단.
#
# 모킹: cs.svc.classify_leveltest_band 를 스크립트된 밴드 시퀀스로 교체 + 천장 게이트
# 상수(TIME_FLOOR/MIN_ANSWERS)를 monkeypatch 로 축소해 결정적으로 천장을 만든다.
# _CLOSE_MARK 는 CLOSE_SEED_LEVELTEST('오늘 대화는 여기까지')로 종료 시드를 식별한다.
# --------------------------------------------------------------------------- #
_CLOSE_MARK = "오늘 대화는 여기까지"  # CLOSE_SEED_LEVELTEST 마커(넛지·[다음] 아님)


def _fake_band(seq):
    """classify_leveltest_band 가짜 — 호출마다 seq 의 다음 값 반환(소진 후 마지막 반복).

    seq: int|None(항상 그 값) 또는 list[int|None](순차, 소진되면 마지막 유지) 또는
         "raise"(항상 예외 — 백스톱 검증). 반환: (async fake, 호출 기록 dict).
    """
    rec: dict = {"n": 0, "args": []}

    async def _f(client, *, answer_text, prior_question=None):
        rec["n"] += 1
        rec["args"].append((answer_text, prior_question))
        if seq == "raise":
            raise RuntimeError("band classify down")
        if isinstance(seq, list):
            return seq[min(rec["n"] - 1, len(seq) - 1)]
        return seq

    return _f, rec


class BandDriver:
    """레벨테스트 밴드 관측 구동용 가짜 Live 세션.

    오프닝 비버 질문 → (유저 답 → 비버 후속 질문) 반복. 매 비버 후속 턴 시작에서
    직전 유저 답변이 관측 사이드카로 발사된다(_spawn_band_observe). 종료 시드
    (CLOSE_SEED_LEVELTEST) 감지 시 작별 턴(FAREWELL 오디오) 후 종료.

    idle_until_close=False: n_answers 소진 후 종료 시드가 안 왔으면 스트림 자연 종료
      (관측이 천장을 못 쳤다 = 조기종료 없음 — hang 없이 빠르게 끝나 검증 가능).
    idle_until_close=True: 소진 후 종료 시드(캡/무음 백스톱)를 기다렸다 작별(백스톱 검증).
    """

    FAREWELL = b"\x55\x55"

    def __init__(self, n_answers: int = 8, answer: str = "저는 서울에 살아요",
                 idle_until_close: bool = False):
        self.sent_audio: list[bytes] = []
        self.sent_text_turns: list[str] = []
        self._n = n_answers
        self._answer = answer
        self._idle_until_close = idle_until_close
        self._close = asyncio.Event()

    async def send_audio(self, pcm16_16k: bytes) -> None:
        self.sent_audio.append(pcm16_16k)

    async def send_text_turn(self, text: str) -> None:
        self.sent_text_turns.append(text)
        if _CLOSE_MARK in text:  # 레벨테스트 종료 시드 주입됨(넛지·[다음] 아님)
            self._close.set()

    async def events(self):
        yield LiveEvent(kind="out_tr", text="이름이 뭐예요?")
        yield LiveEvent(kind="audio", audio=b"\x00\x00")
        yield LiveEvent(kind="turn_end")
        for i in range(self._n):
            if self._close.is_set():
                break
            yield LiveEvent(kind="in_tr", text=f"{self._answer} {i}", is_final=True)
            yield LiveEvent(kind="out_tr", text="그리고 또요?")   # 후속 턴 시작 → 직전 답 관측
            yield LiveEvent(kind="audio", audio=b"\x00\x00")
            yield LiveEvent(kind="turn_end")
            # 관측 사이드카(논블로킹)가 스케줄돼 obs/천장/종료 시드 주입을 마칠 여유.
            for _ in range(5):
                if self._close.is_set():
                    break
                await asyncio.sleep(0.01)
        if self._idle_until_close and not self._close.is_set():
            try:
                await asyncio.wait_for(self._close.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                return
        if not self._close.is_set():
            return  # 천장 미도달 → 스트림 자연 종료(_CallFinished) — 작별 없음
        yield LiveEvent(kind="out_tr", text="결과는 곧 알려줄게요")
        yield LiveEvent(kind="audio", audio=self.FAREWELL)
        yield LiveEvent(kind="turn_end")


# --- 1. advanced 즉시 천장 종료 --------------------------------------------- #
@pytest.mark.asyncio
async def test_band_advanced_reaches_ceiling_and_closes(session_factory, seeded, monkeypatch):
    """관측 band=3(advanced) → obs_max=3 → 천장 → should_close → 종료 시드
    (CLOSE_SEED_LEVELTEST)·작별·call_ended. 게이트 축소(MIN=1·FLOOR=0)로 첫 관측에서 천장.
    3분캡(LEVELTEST_MAX_S 기본 180s) 전에 밴드가 종료를 몬다(캡이면 hang → 여기선 즉시 종료)."""
    monkeypatch.setattr(cs, "LEVELTEST_BAND_TIME_FLOOR_S", 0.0)
    monkeypatch.setattr(cs, "LEVELTEST_BAND_MIN_ANSWERS", 1)
    fake, rec = _fake_band(3)
    monkeypatch.setattr(cs.svc, "classify_leveltest_band", fake)

    sess = BandDriver(n_answers=8)
    ws, _ = await _run_call_with(sess, seeded, session_factory, call_type="level_test")

    assert rec["n"] >= 1, "관측 사이드카가 한 번도 호출되지 않음"
    assert rec["args"][0][0].startswith("저는 서울에 살아요"), "관측에 유저 답변이 캡처되지 않음"
    assert any(_CLOSE_MARK in t for t in sess.sent_text_turns), \
        "advanced 천장 도달했는데 종료 시드(CLOSE_SEED_LEVELTEST) 미주입"
    assert not any(t.startswith("[다음]") for t in sess.sent_text_turns), \
        "관측이 질문을 주입했다('[다음]' — 무주입 위반)"
    assert BandDriver.FAREWELL in ws.sent_bytes, "작별 오디오 미전달(뚝 끊김)"
    assert any('"call_ended"' in t for t in ws.sent_text), "call_ended 미전송"


# --- 2. plateau 종료 -------------------------------------------------------- #
@pytest.mark.asyncio
async def test_band_plateau_reaches_ceiling_and_closes(session_factory, seeded, monkeypatch):
    """관측 band=1 고정(상승 없음) → plateau_count>=PLATEAU_N(3) & obs_count>=MIN(4) →
    천장 → 종료 시드·작별·call_ended. advanced 없이도 '더 못 올라감'으로 조기종료한다."""
    monkeypatch.setattr(cs, "LEVELTEST_BAND_TIME_FLOOR_S", 0.0)
    # MIN_ANSWERS=4·PLATEAU_N=3 기본값 사용(정체 4관측: obs_max=1 후 plateau 3).
    fake, rec = _fake_band(1)
    monkeypatch.setattr(cs.svc, "classify_leveltest_band", fake)

    sess = BandDriver(n_answers=8)
    ws, _ = await _run_call_with(sess, seeded, session_factory, call_type="level_test")

    assert rec["n"] >= 4, f"정체 천장 판정에 필요한 관측(>=4)이 안 쌓임: {rec['n']}"
    assert any(_CLOSE_MARK in t for t in sess.sent_text_turns), \
        "plateau 천장 도달했는데 종료 시드 미주입"
    assert not any(t.startswith("[다음]") for t in sess.sent_text_turns), \
        "관측이 질문을 주입했다('[다음]' — 무주입 위반)"
    assert BandDriver.FAREWELL in ws.sent_bytes, "작별 오디오 미전달"
    assert any('"call_ended"' in t for t in ws.sent_text), "call_ended 미전송"


# --- 3. band None 무변경(조기종료 없음) ------------------------------------- #
@pytest.mark.asyncio
async def test_band_none_never_reaches_ceiling(session_factory, seeded, monkeypatch):
    """classify=None(무응답/판정불가) 반복 → obs_count 안 늘어 obs 천장 절대 안 침(게이트를
    MIN=1·FLOOR=0 으로 최대한 열어도). 비화자 조기종료(NONSPEAKER_MAX)는 도달불가로 막아
    obs 경로만 순수 검증. 밴드발 조기종료 없음 → 종료 시드 미주입, 스트림 자연 종료."""
    monkeypatch.setattr(cs, "LEVELTEST_BAND_TIME_FLOOR_S", 0.0)
    monkeypatch.setattr(cs, "LEVELTEST_BAND_MIN_ANSWERS", 1)
    monkeypatch.setattr(cs, "LEVELTEST_BAND_NONSPEAKER_MAX", 99)  # 비화자 경로 무력화(obs 경로만 검증)
    monkeypatch.setattr(cs, "LEVELTEST_MAX_S", 100.0)  # 캡은 멀리(밴드 무발동을 순수 검증)
    fake, rec = _fake_band(None)
    monkeypatch.setattr(cs.svc, "classify_leveltest_band", fake)

    sess = BandDriver(n_answers=5, idle_until_close=False)
    ws, _ = await _run_call_with(sess, seeded, session_factory, call_type="level_test")

    assert rec["n"] >= 1, "관측이 발사되지 않음(None 이어도 사이드카는 돌아야 함)"
    assert not any(_CLOSE_MARK in t for t in sess.sent_text_turns), \
        "band None 인데 밴드 천장으로 종료 시드가 주입됨(조기종료 오발동)"
    assert not any(t.startswith("[다음]") for t in sess.sent_text_turns), \
        "관측이 질문을 주입했다('[다음]')"


# --- 3b. 비화자 조기종료: None(한국어 산출 없음) 여러 번 → 빨리 종료 시드 ------ #
@pytest.mark.asyncio
async def test_band_nonspeaker_early_closes(session_factory, seeded, monkeypatch):
    """완전 비화자(한국어를 못 해 classify 가 매번 None)라도 NONSPEAKER_MAX 만큼 시도하면
    (FLOOR 경과 후) 비화자 천장으로 조기종료 시드를 주입한다 — 한국어 못 하는 사람이 3분캡
    까지 붙잡히던 역설(call 253, 127s) 차단. obs_count 는 0 이지만 total_answers 로 종료."""
    monkeypatch.setattr(cs, "LEVELTEST_BAND_TIME_FLOOR_S", 0.0)
    monkeypatch.setattr(cs, "LEVELTEST_BAND_NONSPEAKER_MAX", 4)  # 4회 시도면 종료
    monkeypatch.setattr(cs, "LEVELTEST_MAX_S", 100.0)  # 캡은 멀리(비화자 경로만 검증)
    fake, rec = _fake_band(None)
    monkeypatch.setattr(cs.svc, "classify_leveltest_band", fake)

    sess = BandDriver(n_answers=8, idle_until_close=False)
    await _run_call_with(sess, seeded, session_factory, call_type="level_test")

    assert rec["n"] >= 4, f"비화자 관측이 NONSPEAKER_MAX 만큼 안 돎(n={rec['n']})"
    assert any(_CLOSE_MARK in t for t in sess.sent_text_turns), \
        "비화자(None 반복)인데 조기종료 시드 미주입 — 완전초보 빨리 종료 실패"
    assert not any(t.startswith("[다음]") for t in sess.sent_text_turns), \
        "비화자 종료 경로가 질문을 주입했다('[다음]' — 무주입 위반)"


# --- 4. 무주입 유지(회귀): 관측 여러 번에도 '[다음]' 0건 --------------------- #
@pytest.mark.asyncio
async def test_band_observe_never_injects_questions(session_factory, seeded, monkeypatch):
    """★ 관측만·무주입 불변식: 관측이 여러 번 돌고 천장으로 종료돼도 서버가 세션에 넣는
    텍스트 턴은 선톡 시드([통화 시작]) + 종료 시드(CLOSE_SEED_LEVELTEST)뿐 — 질문 마커
    '[다음]' 0건. 관측은 should_close/종료 시드만 세우고 질문을 절대 주입하지 않는다."""
    monkeypatch.setattr(cs, "LEVELTEST_BAND_TIME_FLOOR_S", 0.0)
    fake, rec = _fake_band([1, 2, 2, 3])  # 상승하다 3에서 천장
    monkeypatch.setattr(cs.svc, "classify_leveltest_band", fake)

    sess = BandDriver(n_answers=8)
    await _run_call_with(sess, seeded, session_factory, call_type="level_test")

    turns = sess.sent_text_turns
    assert not any(t.startswith("[다음]") for t in turns), \
        "관측 도중 서버가 질문을 주입했다('[다음]' — 무주입 위반)"
    # 서버 주입 텍스트는 선톡 시드 or 종료 시드뿐(넛지는 미발동 — 무음 없음).
    for t in turns:
        assert t.startswith("[통화 시작]") or _CLOSE_MARK in t, \
            f"예상 밖 서버 텍스트 턴 주입(주입 누수): {t[:40]}"
    assert any(_CLOSE_MARK in t for t in turns), "천장 도달 종료 시드 미주입"


# --- 5. 시간 플로어: FLOOR 미달이면 band=3 여도 조기종료 안 함 --------------- #
@pytest.mark.asyncio
async def test_band_time_floor_blocks_early_close(session_factory, seeded, monkeypatch):
    """천장 시간 플로어 게이트: FLOOR 를 도달불가(9999s)로 두면 band=3 이 쌓여도(MIN=1)
    _band_ceiling_reached 가 elapsed<FLOOR 로 False → 밴드발 종료 시드 없음. 초반 소수
    표본으로 조기종료하지 않음을 보장(종료는 캡/무음이 담당)."""
    monkeypatch.setattr(cs, "LEVELTEST_BAND_TIME_FLOOR_S", 9999.0)  # 도달 불가
    monkeypatch.setattr(cs, "LEVELTEST_BAND_MIN_ANSWERS", 1)
    monkeypatch.setattr(cs, "LEVELTEST_MAX_S", 100.0)  # 캡도 멀리(플로어 게이트만 검증)
    fake, rec = _fake_band(3)
    monkeypatch.setattr(cs.svc, "classify_leveltest_band", fake)

    sess = BandDriver(n_answers=5, idle_until_close=False)
    ws, _ = await _run_call_with(sess, seeded, session_factory, call_type="level_test")

    assert rec["n"] >= 1, "관측이 발사되지 않음"
    assert not any(_CLOSE_MARK in t for t in sess.sent_text_turns), \
        "시간 플로어 미달인데 밴드 천장으로 조기종료됨(플로어 게이트 무력화)"


# --- 6. 백스톱: 관측 사이드카 예외여도 3분캡이 종료를 몬다 ------------------- #
@pytest.mark.asyncio
async def test_band_sidecar_failure_falls_back_to_cap(session_factory, seeded, monkeypatch):
    """R5 백스톱: classify_leveltest_band 가 매번 예외여도 사이드카가 흡수(관측 1건 누락)
    → 통화 무영향. 밴드는 종료를 못 몰지만 3분캡(LEVELTEST_MAX_S)이 종료 시드·작별로
    우아하게 종료한다. 관측 실패가 통화를 죽이지 않음을 보장."""
    monkeypatch.setattr(cs, "LEVELTEST_BAND_TIME_FLOOR_S", 0.0)
    monkeypatch.setattr(cs, "LEVELTEST_BAND_MIN_ANSWERS", 1)
    monkeypatch.setattr(cs, "LEVELTEST_MAX_S", 0.3)   # 3분 캡 → 0.3s
    monkeypatch.setattr(cs, "SEED_TO_HANGUP_S", 3.0)
    fake, rec = _fake_band("raise")
    monkeypatch.setattr(cs.svc, "classify_leveltest_band", fake)

    sess = BandDriver(n_answers=8, idle_until_close=True)
    ws, _ = await _run_call_with(sess, seeded, session_factory, call_type="level_test")

    assert rec["n"] >= 1, "관측이 발사되지 않음(예외라도 사이드카는 돌아야 함)"
    assert any(_CLOSE_MARK in t for t in sess.sent_text_turns), \
        "관측 실패 시 캡 종료 시드가 주입되지 않음(백스톱 실패)"
    assert BandDriver.FAREWELL in ws.sent_bytes, "작별 오디오 미전달(뚝 끊김)"
    assert any('"call_ended"' in t for t in ws.sent_text), "call_ended 미전송"


# --- 7. 일반 통화 무영향: band_observe=False → classify 미호출 -------------- #
@pytest.mark.asyncio
async def test_normal_call_never_observes_band(session_factory, seeded, monkeypatch):
    """일반 통화(band_observe=False)는 밴드 관측 경로를 전혀 밟지 않는다 — 유저가 여러 번
    답해도 classify_leveltest_band 는 0회 호출. 레벨테스트 전용 관측이 일반 통화로 새지
    않음을 보장(격리 회귀)."""
    fake, rec = _fake_band(3)
    monkeypatch.setattr(cs.svc, "classify_leveltest_band", fake)

    sess = BandDriver(n_answers=4, idle_until_close=False)
    await _run_call_with(sess, seeded, session_factory, call_type="normal")

    assert rec["n"] == 0, f"일반 통화에서 밴드 관측이 호출됨(격리 위반): {rec['n']}회"
