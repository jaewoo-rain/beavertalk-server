"""R1-c(2026-09-24, 프론트 실기기 QA) — 프리미엄 다조각 프리토킹이 통화 도중 레벨이
바뀌던 결함. run_call + 가짜 Live 세션 + sqlite 실제 커리큘럼 시드.

증상: 조각1(≥60초, freetalk_done 성립)이 끝나자마자 complete_freetalk 가 그 자리에서
freetalk_done·포인터 전진·L3 레벨업을 찍는다(이건 그대로 둔다 — 실제로 일어난 진행).
조각2·3 은 **같은 call_id** 로 open_call 이 옛 차시(cur_call.lesson_id, 안 바뀜)를
돌려주는데, 시스템 지시문은 새 세션이라 load_call_setup 을 다시 불러 회원의 **방금
오른 새 레벨**로 조립됐다 — 옛 차시 소재를 새 레벨 난이도·code-switching 밴드로 계속
얘기하는 결과가 됐다.

수정: 프리토킹(cur_route) 시스템 지시문의 level_profile 을 회원의 "지금" 레벨이
아니라 **이 통화의 차시**(cur_open.lesson.level_no — cur_call 은 call_id 당 한 번만
만들어져 조각이 바뀌어도 그대로다)로 고정한다(normalcall_service.level_profile_for).
complete_freetalk 의 레벨업 트리거 시점 자체는 안 건드린다 — 조각1에서 이미 정상
확정되므로, 마지막 조각에서 이탈해도 레벨업을 잃을 위험이 없다(방향 (a) "레벨업을
마지막 조각으로 미루기"였다면 이 위험이 있었다 — bt-back 이 지적한 P0-4 류 결함).

무엇을 지키나:
  ① 조각2 는 조각1 과 같은(옛) 레벨 프로파일을 쓴다(레벨업이 있었어도)
  ② 조각1 종료에서 레벨업이 이미 찍힌다(트리거 시점 불변 확인)
  ③ 조각 1개(Free, 이어하기 없음)인 통화는 레벨 프로파일이 종전과 동일
근거: docs/plans/2026-09-12-cur-2단계-통화경로-이전.md · docs/plans/2026-09-23-레벨-커리큘럼-연결.md
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os

import pytest
from sqlalchemy import Integer, create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base
from domains.account.models.member import Member
from domains.account.models.member_reason import MemberReason
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.curriculum import CurMemberLesson, CurMemberProgress
from domains.learning.models.learning_item import LearningItem
from domains.learning.models.level import Level
from domains.learning.realtime.call_session import run_call
import domains.learning.realtime.call_session as cs
import domains.learning.service.normalcall_service as svc
from domains.learning.repository import curriculum_repository as repo
from domains.learning.repository import mastery_repository
from core.config import settings as app_settings
from core.gemini_live import LiveEvent
from scripts.curriculum.load_cur_seed import load

SEED = os.path.join(os.path.dirname(__file__), "..", "assets", "curriculum_v3", "cur_seed.json")
pytestmark = pytest.mark.skipif(not os.path.exists(SEED), reason="cur_seed.json 없음")


@pytest.fixture(scope="module")
def session_factory():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            if len(t.primary_key.columns) == 1:
                pk.type = Integer()
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = sf()
    try:
        for i in range(46):
            db.add(LearningItem(language="ko", kind="chunk", source_key=f"c:{i}", band=1, level_no=1, assign_rule="seed",
                                surface=f"청크 문장 {i}", meanings=json.dumps({"en": f"chunk {i}"}), examples="[]"))
        db.commit()
        load(db, json.load(io.open(SEED, encoding="utf-8")), dry_run=False)
        db.commit()
        v = Voice(name="Fenrir", gender="male"); db.add(v); db.flush()
        db.add(Character(name="비비", role="친근한 선생님", personality="다정함", voice_id=v.voice_id, price=0))
        # ⭐ R1-c — load_cur_seed 가 청크 차시 3개(no=1~3, 레벨1)를 앞에 끼우고 나머지를
        #   +3 밀어 담으므로, 실측(DB 직접 조회)상 레벨2(A1)는 no=4~20·레벨3(A2)는
        #   no=21 부터다. 경계(no=20→21)를 걸치도록 레벨2·3 프로파일을 서로 다른
        #   문구로 둔다(어느 쪽이 프롬프트에 실렸는지 문자열로 가른다).
        db.add(Level(language="ko", level_no=2, profile="레벨2-마커-프로파일"))
        db.add(Level(language="ko", level_no=3, profile="레벨3-마커-프로파일"))
        db.commit()
    finally:
        db.close()
    return sf


_n = {"i": 0}


@pytest.fixture()
def seeded(session_factory):
    _n["i"] += 1
    db = session_factory()
    try:
        ch = db.execute(text("SELECT character_id FROM character LIMIT 1")).scalar()
        # 명시 freetalk 라우팅(admin 전용 게이트)을 그대로 타려고 admin 으로 둔다 —
        # test_cur_call_path.py 와 같은 관례. 레벨2(A1) 마지막 차시(no=20)에서 시작해
        # 완료하면 레벨3(A2) 첫 차시(no=21)로 경계를 넘는다.
        m = Member(language="en", korean_level=2, onboarding_completed=True,
                  auth_user_id=f"auth-ftk-pin-{_n['i']}", role="admin")
        db.add(m); db.flush()
        db.add(MemberReason(member_id=m.member_id, reason="travel"))
        lesson1 = repo.lesson_by_no(db, "ko", 20)
        db.add(CurMemberProgress(member_id=m.member_id, language="ko", lesson_id=lesson1.lesson_id))
        # 표현학습을 드릴하지 않고 바로 «표현학습 완료» 상태로 만들어 프리토킹을 연다.
        from datetime import datetime, timezone
        db.add(CurMemberLesson(member_id=m.member_id, lesson_id=lesson1.lesson_id,
                                status="expression_done", expression_done_at=datetime.now(timezone.utc)))
        db.commit()
        return {"member_id": m.member_id, "character_id": ch, "lesson1_id": lesson1.lesson_id}
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
    # 가짜 통화(<1초)도 «프리토킹 완료» 문턱을 넘게 — 조각1의 진짜 완료를 봐야 하는 시험이다.
    monkeypatch.setattr(cs.cur_svc, "FREETALK_MIN_DURATION_S", 0.0)
    # Free 는 조각 1 — 이어하기 관문을 Pro 상당(3)으로 열어 조각2 를 재현할 수 있게 한다.
    monkeypatch.setattr(cs.call_service, "call_fragments_for_member", lambda db, m: 3)


class FakeWebSocket:
    def __init__(self, incoming, hold_until=None, deferred=None):
        self._incoming = list(incoming)
        self._hold_until = hold_until
        self._deferred = list(deferred or [])
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closed_with: int | None = None
        from starlette.websockets import WebSocketState
        self._WS = WebSocketState
        self.client_state = WebSocketState.CONNECTED

    async def receive(self) -> dict:
        if self._incoming:
            return self._incoming.pop(0)
        if self._deferred:
            pred, msg = self._deferred[0]
            for _ in range(400):
                if pred():
                    self._deferred.pop(0)
                    return msg
                await asyncio.sleep(0.01)
        for _ in range(400):
            if self._hold_until is None or self._hold_until():
                break
            await asyncio.sleep(0.01)
        return {"type": "websocket.disconnect"}

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def close(self, code: int | None = None) -> None:
        self.closed_with = code
        self.client_state = self._WS.DISCONNECTED


class FakeLiveSession:
    def __init__(self, script):
        self.sent_text_turns: list[str] = []
        self.script = script

    async def send_audio(self, pcm16_16k: bytes) -> None:
        pass

    async def send_text_turn(self, text: str) -> None:
        self.sent_text_turns.append(text)

    async def send_reground(self, text: str, *, turn_complete: bool = True) -> None:
        self.sent_text_turns.append(text)

    async def events(self):
        for role, txt in self.script:
            if role == "B":
                yield LiveEvent(kind="out_tr", text=txt)
                yield LiveEvent(kind="turn_end")
            else:
                yield LiveEvent(kind="in_tr", text=txt, is_final=True)


class HeldOpenSession(FakeLiveSession):
    """대본 소진 뒤에도 세션을 열어 둔다 — 클라가 fragment_end 를 보내 끝내는 시나리오용."""

    def __init__(self, script):
        super().__init__(script)
        self.script_done = False

    async def events(self):
        async for ev in super().events():
            yield ev
        self.script_done = True
        for _ in range(400):
            await asyncio.sleep(0.01)


def _factory(holder, script=None, session_cls=FakeLiveSession):
    @contextlib.asynccontextmanager
    async def _f(client, settings, *, system_instruction, voice, **_kw):
        sess = session_cls(script or [("B", "안녕!")])
        holder["session"] = sess
        holder["system_instruction"] = system_instruction
        yield sess
    return _f


async def _run(session_factory, seeded, call_type, holder, *, script=None, continues=None,
               extra=None, session_cls=FakeLiveSession, fragment_end=False):
    start = {"type": "start", "character_id": seeded["character_id"], **(extra or {})}
    if call_type is not None:
        start["call_type"] = call_type
    if continues is not None:
        start["continues_call_id"] = str(continues)
    deferred = []
    hold = None
    if fragment_end:
        deferred.append((lambda: bool(holder.get("session") and getattr(holder["session"], "script_done", False)),
                         {"type": "websocket.receive", "text": json.dumps({"type": "fragment_end"})}))
        hold = lambda: holder.get("ws") is not None and holder["ws"].closed_with is not None
    ws = FakeWebSocket([{"type": "websocket.receive", "text": json.dumps(start)}], hold_until=hold, deferred=deferred)
    holder["ws"] = ws
    await run_call(ws, app_settings, object(), session_factory,
                   member_id=seeded["member_id"], live_session_factory=_factory(holder, script, session_cls))
    for _ in range(300):
        if not cs._analysis_tasks:
            break
        await asyncio.sleep(0.01)
    holder["ws"] = ws
    holder["frames"] = [json.loads(t) for t in ws.sent_text]
    return holder


def _started(holder):
    return next(f for f in holder["frames"] if f.get("type") == "call_started")


def _last_call(db, member_id):
    return db.query(Call).filter(Call.member_id == member_id).order_by(Call.call_id.desc()).first()


@pytest.mark.asyncio
async def test_fragment_two_keeps_fragment_ones_level_after_a_level_up(session_factory, seeded):
    """⛔⛔ 핵심 — 조각1 이 레벨을 올려도(정상 확정, ②) 조각2 의 시스템 지시문은
    조각1 과 같은(옛) 레벨 프로파일을 쓴다(①)."""
    m = seeded["member_id"]
    lesson1_id = seeded["lesson1_id"]

    h1: dict = {}
    await _run(
        session_factory, seeded, "freetalk", h1,
        script=[("B", "오늘은 자유롭게 이야기해요."), ("U", "네 좋아요.")],
        session_cls=HeldOpenSession, fragment_end=True,
    )
    assert _started(h1)["course"] == "freetalk"
    assert "레벨2-마커-프로파일" in h1["system_instruction"]
    assert "레벨3-마커-프로파일" not in h1["system_instruction"], "조각1은 아직 레벨업 전이라 레벨2 이어야 한다"

    db = session_factory()
    try:
        call = _last_call(db, m)
        call_id = call.call_id
        # ② 조각1 종료에서 레벨업이 이미 찍힌다(complete_freetalk 트리거 시점 불변 확인).
        assert mastery_repository.get_language_level(db, m, "ko") == 3, \
            "조각1(정상 완료)에서 레벨업이 안 찍혔다 — complete_freetalk 트리거 시점이 바뀌면 안 된다"
        lesson2_id = repo.current_progress(db, m).lesson_id
        assert lesson2_id != lesson1_id, "진도가 다음 차시로 전진해야 한다"
    finally:
        db.close()

    h2: dict = {}
    await _run(
        session_factory, seeded, None, h2,
        script=[("B", "이어서 할게요.")], continues=call_id,
        extra={"silent_resume": True},
    )
    assert _started(h2)["call_id"] == str(call_id)
    assert _started(h2)["course"] == "freetalk"
    # ① 핵심 — 조각2 는 회원의 새 레벨(3)이 아니라 조각1 의 차시(레벨2)를 그대로 쓴다.
    assert "레벨2-마커-프로파일" in h2["system_instruction"], \
        "조각2 가 옛(조각1) 레벨 프로파일을 안 썼다 — R1-c 재발"
    assert "레벨3-마커-프로파일" not in h2["system_instruction"], \
        "조각2 가 회원의 방금 오른 새 레벨을 썼다 — R1-c 재발(통화 도중 레벨이 바뀐다)"


@pytest.mark.asyncio
async def test_a_single_fragment_free_call_is_unaffected(session_factory, seeded):
    """③ 회귀 — 조각 1개(이어하기 없음)인 통화는 레벨 프로파일이 종전과 동일(레벨1)."""
    h: dict = {}
    await _run(
        session_factory, seeded, "freetalk", h,
        script=[("B", "오늘은 자유롭게 이야기해요."), ("B", "네, 좋아요.")],
    )
    assert _started(h)["course"] == "freetalk"
    assert "레벨2-마커-프로파일" in h["system_instruction"]
    assert "레벨3-마커-프로파일" not in h["system_instruction"]
