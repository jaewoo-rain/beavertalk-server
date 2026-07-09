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

    monkeypatch.setattr(svc.tts, "synthesize_korean", _fake_tts)

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

    monkeypatch.setattr(svc.tts, "synthesize_korean", _slow_failing_tts)

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
