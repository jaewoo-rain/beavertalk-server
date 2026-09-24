"""알람별 통화 모드 — call_session 라우팅 회귀 (프론트 요청 #1, 2026-09-23).

무엇을 지키나:
  ① inbound_call_id 로 시작한 통화는 **알람의 call_type** 을 따른다 — start.call_type
     이 함께 와도(앱은 알람 통화에서도 홈 버튼 값을 그대로 실어 보낸다).
  ② 레벨 미확정 회원의 알람 통화는 여전히 level_test(알람 모드가 덮지 않는다).
  ③ 알람 조회는 캐릭터 해석과 **한 번**의 쿼리로 같이 나간다(resolve_call_character 를
     두 번 부르거나 새 조회를 추가하지 않았는지).

가짜 Live 세션 + 가짜 WS + 인메모리 DB. 네트워크 0. 헬퍼는 tests/test_level_test_call.py ·
tests/test_call_character_resolution.py 의 패턴을 그대로 모방한다(두 파일 수정 금지).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401 - 전 모델 import
from domains.account.models.member import Member
from domains.account.models.member_reason import MemberReason
from domains.alarm.models.alarm import Alarm
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.level import Level
from domains.push.models.push_dispatch_log import PushDispatchLog

from core.config import settings as app_settings
from core.gemini_live import LiveEvent

import domains.learning.realtime.call_session as cs
import domains.learning.service.normalcall_service as svc
from domains.learning.realtime.call_session import run_call


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
    db = session_factory()
    try:
        voice = Voice(name="Fenrir", gender="male")
        db.add(voice)
        db.flush()
        ch = Character(name="비비", role="친근한 선생님", personality="다정함",
                       voice_id=voice.voice_id, price=0)
        db.add(ch)
        db.add(Level(language="ko", level_no=1, profile="생존 회화"))
        db.flush()
        # 레벨 확정 회원(alarm→chat/auto 정상 라우팅) + 미확정 회원(level_test 보호).
        with_level = Member(language="en", korean_level=1, onboarding_completed=True,
                            auth_user_id="auth-with-level")
        no_level = Member(language="en", korean_level=None, onboarding_completed=True,
                          auth_user_id="auth-no-level")
        db.add_all([with_level, no_level])
        db.flush()
        db.add(MemberReason(member_id=no_level.member_id, reason="travel"))
        db.commit()
        return {
            "member_id": with_level.member_id,
            "member_no_level": no_level.member_id,
            "character_id": ch.character_id,
        }
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _mock_external(monkeypatch):
    monkeypatch.setattr(svc.storage, "upload", lambda *a, **k: "stub-key")
    monkeypatch.setattr(svc.storage, "public_url", lambda *a, **k: "https://stub/url.mp3")

    async def _fake_tts(*_a, **_k):
        return None

    monkeypatch.setattr(svc.tts, "synthesize", _fake_tts)

    async def _fake_generate(*_a, **_k):
        return svc.CallAnalysis(summary="요약", detected_mode="chat", expressions=[])

    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", _fake_generate)


class FakeWebSocket:
    def __init__(self, incoming: list[dict]):
        self._incoming = list(incoming)
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        from starlette.websockets import WebSocketState
        self._WS = WebSocketState
        self.client_state = WebSocketState.CONNECTED

    async def receive(self) -> dict:
        if self._incoming:
            return self._incoming.pop(0)
        return {"type": "websocket.disconnect"}

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def close(self, code: int | None = None) -> None:
        self.client_state = self._WS.DISCONNECTED


class FakeLiveSession:
    def __init__(self):
        self.sent_text_turns: list[str] = []

    async def send_audio(self, pcm16_16k: bytes) -> None:  # pragma: no cover - 미사용
        pass

    async def send_text_turn(self, text: str) -> None:
        self.sent_text_turns.append(text)

    async def events(self):
        yield LiveEvent(kind="out_tr", text="안녕!")
        yield LiveEvent(kind="turn_end")


def _factory(holder):
    @contextlib.asynccontextmanager
    async def _f(client, settings, *, system_instruction, voice, **_kw):
        sess = FakeLiveSession()
        holder["session"] = sess
        holder["system_instruction"] = system_instruction
        yield sess

    return _f


def _dispatched(session_factory, member_id: int, character_id: int, call_id: str,
                call_type: str = "auto") -> None:
    """이 회원에게 `call_type` 모드로 알람 전화를 발송한 상태를 만든다."""
    db = session_factory()
    try:
        a = Alarm(member_id=member_id, character_id=character_id,
                  time=datetime.now(timezone.utc), is_activate=True, call_type=call_type)
        db.add(a)
        db.flush()
        db.add(PushDispatchLog(alarm_id=a.alarm_id,
                               intended_fire_minute="2026-09-23 08:00",
                               call_id=call_id))
        db.commit()
    finally:
        db.close()


async def _run(session_factory, seeded, *, call_type: str | None, inbound_call_id: str | None,
               member_id: int | None = None) -> dict:
    holder: dict = {}
    start: dict = {"type": "start", "character_id": seeded["character_id"]}
    if call_type is not None:
        start["call_type"] = call_type
    if inbound_call_id is not None:
        start["inbound_call_id"] = inbound_call_id
    ws = FakeWebSocket([{"type": "websocket.receive", "text": json.dumps(start)}])
    await run_call(
        ws, app_settings, object(), session_factory,
        member_id=member_id or seeded["member_id"],
        live_session_factory=_factory(holder),
    )
    for _ in range(300):
        if not cs._analysis_tasks:
            break
        await asyncio.sleep(0.01)
    return holder


# --------------------------------------------------------------------------- #
# ① 알람 call_type 이 start.call_type 보다 우선한다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_alarm_chat_wins_even_when_start_sends_auto(session_factory, seeded):
    """★ 핵심 — 앱이 홈 버튼 값("auto")을 그대로 실어 보내도 알람의 chat 이 이긴다."""
    _dispatched(session_factory, seeded["member_id"], seeded["character_id"],
                "call-chat-1", call_type="chat")
    await _run(session_factory, seeded, call_type="auto", inbound_call_id="call-chat-1")
    db = session_factory()
    try:
        assert db.query(Call).one().call_type == "chat"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_alarm_auto_wins_even_when_start_sends_chat(session_factory, seeded):
    """대칭 케이스 — 알람이 auto(학습)면 start.call_type="chat" 이어도 학습으로 간다."""
    _dispatched(session_factory, seeded["member_id"], seeded["character_id"],
                "call-auto-1", call_type="auto")
    await _run(session_factory, seeded, call_type="chat", inbound_call_id="call-auto-1")
    db = session_factory()
    try:
        # 이 시드 DB 엔 cur_lesson 이 없어 auto 는 옛 표현학습 경로로 폴백한다(expression).
        assert db.query(Call).one().call_type == "expression"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_alarm_call_type_applies_even_when_start_omits_call_type(session_factory, seeded):
    """알람 통화에서 start.call_type 자체를 안 보내도(구버전) 알람 값이 적용된다."""
    _dispatched(session_factory, seeded["member_id"], seeded["character_id"],
                "call-chat-2", call_type="chat")
    await _run(session_factory, seeded, call_type=None, inbound_call_id="call-chat-2")
    db = session_factory()
    try:
        assert db.query(Call).one().call_type == "chat"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# ② 레벨 미확정이면 여전히 level_test — 알람 모드가 덮지 않는다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_needs_level_test_still_wins_over_alarm_chat(session_factory, seeded):
    """⛔ 알람이 이기는 건 auto·chat 사이에서만이다 — level_test 판정은 그 위에 있다."""
    _dispatched(session_factory, seeded["member_no_level"], seeded["character_id"],
                "call-lt-1", call_type="chat")
    await _run(
        session_factory, seeded, call_type=None, inbound_call_id="call-lt-1",
        member_id=seeded["member_no_level"],
    )
    db = session_factory()
    try:
        assert db.query(Call).one().call_type == "level_test", \
            "알람의 chat 이 레벨테스트를 덮었다"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_needs_level_test_still_wins_over_alarm_chat_with_explicit_auto(session_factory, seeded):
    """⭐⭐ L8(2026-09-24) — 위 시험과 같지만 start.call_type="auto" 를 **명시로** 보낸다
    (앱이 실제로 보내는 값). L8 이전엔 명시 "auto" 가 레벨테스트 판정을 안 거쳐 알람의
    chat 이 그대로 이겼다 — 이 시험이 바로 그 버그를 잠근다."""
    _dispatched(session_factory, seeded["member_no_level"], seeded["character_id"],
                "call-lt-2", call_type="chat")
    await _run(
        session_factory, seeded, call_type="auto", inbound_call_id="call-lt-2",
        member_id=seeded["member_no_level"],
    )
    db = session_factory()
    try:
        assert db.query(Call).one().call_type == "level_test", \
            "명시 auto + 알람의 chat 조합에서 레벨테스트가 안 이겼다(L8 회귀)"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# ③ 알람 조회는 한 번만 — resolve_call_character 호출 1회로 캐릭터+모드를 같이 얻는다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_resolve_call_character_is_called_exactly_once(session_factory, seeded, monkeypatch):
    calls: list = []
    orig = svc.resolve_call_character

    def _spy(db, member_id, inbound_call_id=None):
        calls.append(inbound_call_id)
        return orig(db, member_id, inbound_call_id)

    monkeypatch.setattr(cs.svc, "resolve_call_character", _spy)
    _dispatched(session_factory, seeded["member_id"], seeded["character_id"],
                "call-once-1", call_type="chat")
    await _run(session_factory, seeded, call_type="auto", inbound_call_id="call-once-1")
    assert calls == ["call-once-1"], \
        f"알람 캐릭터+모드 해석이 두 번 이상 쿼리됐다: {calls}"
