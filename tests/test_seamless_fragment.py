"""끊김 없는 5분 조각 전환 — 서버 몫 S1~S5 (계획 docs/plans/2026-09-13-끊김없는-조각-전환.md).

클라가 5:00 뒤 «학습자 발화→비버 응답 turn_end» 에서 소켓을 닫고 즉시 `continues_call_id + silent_resume:true` 로 다시 연다.
서버: ① start.silent_resume(기본 False) ② 재개 조각이면 **시드 0**(비버는 학습자 첫 발화를 기다린다 — 사장님 결정 2) + 브리프 마지막 줄을
silent 용 첫 행동 지정으로 ③ 무음 시계 기준점 = 세션 열기 시각(학습자가 끝내 말 안 하면 무음 3단으로 종료 — 결정 1) ④ call_started 에
fragment_index·max_fragments(None 은 직렬화 제거 → 구클라 프레임 바이트 동일) ⑤ 기존 경로(시드·브리프·프레임)는 바이트 불변.

수용 기준(§4) ↔ 시험:
  · 조각 경계에서 비버 발화 0(조각2 첫 턴은 학습자 발화 뒤에만) → test_silent_resume_sends_no_seed_and_pins_the_first_action_line /
    test_silent_resume_on_an_expression_call_sends_no_seed_and_appends_the_silent_note
  · 학습자가 끝내 말 안 하면 무음 3단으로 종료(540s 백스톱에 매달리지 않음) → test_silent_resume_starts_the_idle_clock_at_session_open
  · 마지막 조각 판단은 서버 값으로 → test_call_started_carries_fragment_index_and_max_fragments
  · Free·구클라 종전과 프레임 동일 → test_call_started_frame_is_byte_identical_when_fragment_fields_are_none /
    test_silent_resume_without_a_resumable_call_falls_back_to_the_normal_opening
  · 기존 이어하기(silent 아님) 바이트 불변 → test_non_silent_resume_is_unchanged + tests/test_prompt_locked_hash.py
  · (QA P1-A, S6) 조각 경계 레이스 0 — 클라 fragment_end → 저장(판정·진도·전사) 뒤 fragment_saved, call_ended 미전송, turn_index 충돌 0
    → test_fragment_end_saves_the_fragment_before_fragment_saved_and_sends_no_call_ended /
      test_fragment_end_on_an_expression_call_records_progress_before_fragment_saved
  · (S6) 종전 close 경로 프레임 불변(call_ended 그대로) → test_the_old_close_path_still_sends_call_ended
  · (QA P1-B, S7) 재연결 소켓에 마이크 프레임이 start 보다 먼저 와도 이어하기 유실 0 → test_initial_start_window_ignores_binary_frames
  · (QA P2, S8) T22 2세대 브리프가 silent 조각에서 비버를 먼저 말하게 하지 않는다(기존 문자열 바이트 불변) → test_reconnect_brief_waits_in_a_silent_fragment
  · fragment_end 는 레벨테스트에서 무시 → test_fragment_end_is_ignored_on_a_level_test
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
from domains.learning.models.learning_item import LearningItem
from domains.learning.models.level import Level
from domains.learning.realtime.call_session import run_call
import domains.learning.realtime.call_session as cs
import domains.learning.service.normalcall_service as svc
from domains.learning.realtime.protocol import ClientStart, ServerCallStarted, client_adapter, server_adapter
from core.config import settings as app_settings
from core.gemini_live import LiveEvent
from core.prompts.locked import reground, seeds
from scripts.curriculum.load_cur_seed import load

SEED = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "curriculum_v3", "cur_seed.json")
pytestmark = pytest.mark.skipif(not os.path.exists(SEED), reason="cur_seed.json 없음")


# --------------------------------------------------------------------------- #
# 고정물 — tests/test_cur_call_path.py 와 같은 꼴(sqlite 메모리 + 실제 ko 시드 → 표현학습 코스도 돈다)
# --------------------------------------------------------------------------- #
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
        db.add(Level(language="ko", level_no=1, profile="초급 학습자"))
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
        m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id=f"auth-seamless-{_n['i']}")
        db.add(m); db.flush()
        db.add(MemberReason(member_id=m.member_id, reason="travel"))
        db.commit()
        return {"member_id": m.member_id, "character_id": ch}
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

    # ⛔ flake 원인(3회 중 1회, 2026-09-14): 위 가짜 generate 가 **이어하기 요약**(ResumeOut) 호출에도 CallAnalysis 를 돌려줘 summarize 가
    #   {topic:"", facts:[], pending:""} 를 만들고, 호출부가 «슬롯이 생겼다» 고 보고 발췌(excerpt)를 지운다 → 조각1 분석(call.summary)이
    #   아직 안 착지한 순서면 브리프가 텅 빈다. 요약은 None(실패) 으로 고정해 발췌 폴백이 늘 살게 한다 — 하네스 문제지 서버 경합이 아니다.
    async def _no_summary(*_a, **_k):
        return None
    monkeypatch.setattr(svc, "summarize_for_resume_text", _no_summary)
    # 시드 회원은 Free(조각 1) — 이어하기 관문을 Pro 상당(3)으로 연다. ⚠ monkeypatch 로만(다른 시험으로 새지 않게).
    monkeypatch.setattr(cs.call_service, "call_fragments_for_member", lambda db, m: 3)


class FakeWebSocket:
    def __init__(self, incoming: list[dict], hold_until=None, deferred=None, on_send=None):
        self._incoming = list(incoming)
        self._hold_until = hold_until              # 있으면 참이 될 때까지 disconnect 를 내지 않는다(클라가 붙어 있는 상태를 흉내)
        self._deferred = list(deferred or [])      # (predicate, message): incoming 이 비면 predicate 가 참이 될 때 그 메시지를 낸다
        self._on_send = on_send                    # 서버가 텍스트 프레임을 보낼 때 부르는 훅(저장 순서 관측)
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
                await asyncio.sleep(0.05)
        for _ in range(400):
            if self._hold_until is None or self._hold_until():
                break
            await asyncio.sleep(0.05)
        return {"type": "websocket.disconnect"}

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)
        if self._on_send is not None:
            self._on_send(text)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def close(self, code: int | None = None) -> None:
        self.closed_with = code if code is not None else 1000
        self.client_state = self._WS.DISCONNECTED


class FakeLiveSession:
    """script = (role, text) 목록. B = 비버 턴(out_tr → turn_end), U = 학습자 전사."""

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


class SilentLearnerSession(FakeLiveSession):
    """학습자가 **끝내 말하지 않는** 조각 — 이벤트 0. 서버가 시드(넛지·작별)를 3개 넣으면 세션이 끝난다(작별 턴 대신)."""

    async def events(self):
        for _ in range(400):                       # 상한 20s — 여기 걸리면 무음 시계가 안 돈 것이다
            if len(self.sent_text_turns) >= 3:
                return
            await asyncio.sleep(0.05)
        if False:                                  # noqa — 비동기 제너레이터로 만들기 위한 yield(이벤트는 0건: 비버도 학습자도 말하지 않는다)
            yield LiveEvent(kind="turn_end")


class HeldOpenSession(FakeLiveSession):
    """대본을 다 낸 뒤에도 세션을 열어 둔다(실제 Live 처럼) — 클라가 fragment_end 를 보내 끝내는 시나리오용."""

    def __init__(self, script):
        super().__init__(script)
        self.script_done = False

    async def events(self):
        async for ev in super().events():
            yield ev
        self.script_done = True
        for _ in range(400):                        # 20s 상한 — fragment_end 가 세대를 내리면 TaskGroup 취소로 여기서 끊긴다
            await asyncio.sleep(0.05)


def _factory(holder, script=None, session_cls=FakeLiveSession):
    @contextlib.asynccontextmanager
    async def _f(client, settings, *, system_instruction, voice, **_kw):
        sess = session_cls(script or [("B", "안녕!")])
        holder["session"] = sess
        holder["system_instruction"] = system_instruction
        yield sess
    return _f


async def _run(session_factory, seeded, call_type, holder, *, script=None, continues=None, extra=None, session_cls=FakeLiveSession,
               fragment_end=False, before_start=None, on_send=None):
    start = {"type": "start", "character_id": seeded["character_id"], **(extra or {})}
    if call_type is not None:
        start["call_type"] = call_type
    if continues is not None:
        start["continues_call_id"] = str(continues)
    hold = (lambda: len(holder.get("session").sent_text_turns) >= 3 if holder.get("session") else False) \
        if session_cls is SilentLearnerSession else None
    deferred = []
    if fragment_end:
        # 클라: 비버 마지막 응답 turn_end 를 본 뒤 fragment_end 를 보낸다(소켓은 열어 둔다) — 서버가 fragment_saved 뒤 닫는다
        deferred.append((lambda: bool(holder.get("session") and getattr(holder["session"], "script_done", False)),
                         {"type": "websocket.receive", "text": json.dumps({"type": "fragment_end"})}))
        hold = lambda: holder.get("ws") is not None and holder["ws"].closed_with is not None
    incoming = list(before_start or []) + [{"type": "websocket.receive", "text": json.dumps(start)}]
    ws = FakeWebSocket(incoming, hold_until=hold, deferred=deferred, on_send=on_send)
    holder["ws"] = ws
    await run_call(ws, app_settings, object(), session_factory,
                   member_id=seeded["member_id"], live_session_factory=_factory(holder, script, session_cls))
    for _ in range(300):
        if not cs._analysis_tasks:
            break
        await asyncio.sleep(0.01)
    holder["ws"] = ws
    holder["frames"] = [json.loads(t) for t in ws.sent_text]
    holder["started_raw"] = next(t for t in ws.sent_text if json.loads(t).get("type") == "call_started")
    return holder


def _started(holder):
    return next(f for f in holder["frames"] if f.get("type") == "call_started")


def _last_call(db, member_id):
    return db.query(Call).filter(Call.member_id == member_id).order_by(Call.call_id.desc()).first()


# --------------------------------------------------------------------------- #
# S1 · S4 — 프로토콜
# --------------------------------------------------------------------------- #
def test_client_start_silent_resume_defaults_false_and_parses():
    base = {"type": "start", "character_id": 1, "continues_call_id": "12"}
    m = client_adapter.validate_python(base)
    assert isinstance(m, ClientStart) and m.silent_resume is False, "구클라(필드 없음) = 종전 이어하기"
    assert client_adapter.validate_python({**base, "silent_resume": True}).silent_resume is True
    assert cs.StartParams(None, None, None, None, None).silent_resume is False, "StartParams 기본값 — 기존 호출부·시험 보호"


def test_call_started_frame_is_byte_identical_when_fragment_fields_are_none():
    """⛔ 레벨테스트·구경로 프레임은 키 자체가 없어야 한다(None 직렬화 제거) — 구클라 프레임 바이트 동일."""
    plain = server_adapter.dump_json(ServerCallStarted(character_id=1, call_id="5", diag="summary"))
    assert plain == b'{"type":"call_started","character_id":1,"call_id":"5","diag":"summary"}'
    with_course = server_adapter.dump_json(ServerCallStarted(character_id=1, call_id="5", diag="summary", course="expression"))
    assert with_course == b'{"type":"call_started","character_id":1,"call_id":"5","diag":"summary","course":"expression"}'
    full = json.loads(server_adapter.dump_json(ServerCallStarted(character_id=1, call_id="5", diag="summary", fragment_index=2, max_fragments=3)))
    assert full["fragment_index"] == 2 and full["max_fragments"] == 3


# --------------------------------------------------------------------------- #
# S2 — 브리프 마지막 줄(잠금) · 기존 출력 바이트 불변
# --------------------------------------------------------------------------- #
def test_resume_brief_silent_swaps_only_the_last_line_and_survives_an_empty_brief():
    mats = dict(covered=["물"], strong=["가다"], weak=["-고 싸다"], topic="축구", pending="예문", facts=["학생"], summary="인사", curious="음식")
    plain = reground.build_resume_brief(**mats)
    silent = reground.build_resume_brief(**mats, silent=True)
    assert plain.splitlines()[:-1] == silent.splitlines()[:-1], "맥락 줄은 그대로 — 마지막 줄만 다르다"
    assert silent.splitlines()[-1] == reground.RESUME_SILENT_FIRST_ACTION
    assert plain.splitlines()[-1].startswith("⛔ 처음 만난 것처럼 인사하지 말고"), "silent=False 는 종전 문장(해시 시험이 바이트를 지킨다)"
    for banned in ("끊겼다고", "이어서 할게", "재연결", "다시 연결"):
        assert banned not in reground.RESUME_SILENT_FIRST_ACTION
    assert "먼저 말을 꺼내지 말고 기다렸다가" in reground.RESUME_SILENT_FIRST_ACTION
    # 줄 게 없으면: 종전은 빈 문자열(빈 껍데기 주입 금지) · silent 는 첫 행동 한 줄만(이 줄이 «처음 인사» 를 막는 유일한 문장)
    assert reground.build_resume_brief() == ""
    assert reground.build_resume_brief(silent=True) == reground.RESUME_SILENT_FIRST_ACTION
    # 표현학습 silent 쪽지 — 목록 맨 앞부터·기다림·끊김 언급 금지·모국어
    note = seeds.brief_expression_silent_resume("한국어")
    assert "[오늘의 표현] 목록의 **맨 앞 항목부터**" in note and "기다렸다가" in note and "통화가 끊겼다 이어졌다는 말은 하지 마라" in note
    assert "모국어로 해라" in note


# --------------------------------------------------------------------------- #
# S2 · S4 — 일반 통화 조각1 → 조각2(silent) 통합
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_silent_resume_sends_no_seed_and_pins_the_first_action_line(session_factory, seeded):
    h1 = await _run(session_factory, seeded, "normal", {}, script=[("B", "안녕! 오늘 뭐 했어?"), ("U", "학교에 갔어요"), ("B", "좋아요!")])
    assert h1["session"].sent_text_turns and "[통화종료" not in h1["session"].sent_text_turns[0], "조각1 은 종전 선톡 시드"
    cid = int(_started(h1)["call_id"])
    h2 = await _run(session_factory, seeded, "normal", {}, script=[("U", "네, 계속해요"), ("B", "그래서 학교에서는?")],
                    continues=cid, extra={"silent_resume": True})
    assert _started(h2)["call_id"] == str(cid), "같은 통화 행에 조각2"
    # ⛔ 조각 경계에서 비버 발화 0 — 서버가 세션에 넣은 텍스트 턴이 없다(재개 시드 없음; 넛지도 없음)
    assert h2["session"].sent_text_turns == [], h2["session"].sent_text_turns
    si = h2["system_instruction"]
    assert si.rstrip().endswith(reground.RESUME_SILENT_FIRST_ACTION), "브리프 마지막 줄 = silent 첫 행동 지정"
    assert "⛔ 처음 만난 것처럼 인사하지 말고, 위 흐름을" not in si, "종전 마지막 줄은 silent 에 나가지 않는다"
    assert seeds.seed_resume("한국어") not in si
    db = session_factory()
    try:
        assert db.query(Call).filter(Call.member_id == seeded["member_id"]).count() == 1
        assert (_last_call(db, seeded["member_id"]).fragment_count or 1) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_non_silent_resume_is_unchanged(session_factory, seeded):
    """⛔ 회귀 — silent_resume 를 안 보낸(구클라·이어하기 시트) 조각2 는 종전대로 seed_resume 1턴 + 종전 마지막 줄."""
    h1 = await _run(session_factory, seeded, "normal", {}, script=[("B", "안녕!"), ("U", "안녕하세요"), ("B", "좋아요!")])
    cid = int(_started(h1)["call_id"])
    h2 = await _run(session_factory, seeded, "normal", {}, script=[("B", "그래서요?")], continues=cid)
    assert h2["session"].sent_text_turns[0] == seeds.seed_resume("한국어")
    si = h2["system_instruction"]
    assert "⛔ 처음 만난 것처럼 인사하지 말고, 위 흐름을 **자연스럽게 이어서** 말해라." in si
    assert reground.RESUME_SILENT_FIRST_ACTION not in si


@pytest.mark.asyncio
async def test_call_started_carries_fragment_index_and_max_fragments(session_factory, seeded):
    """클라는 «fragment_index == max_fragments → 마지막 조각(재연결 없음)» 을 이 두 값으로 판단한다(결정 6)."""
    h1 = await _run(session_factory, seeded, "normal", {}, script=[("B", "안녕!"), ("U", "네")])
    s1 = _started(h1)
    assert (s1["fragment_index"], s1["max_fragments"]) == (1, 3)
    cid = int(s1["call_id"])
    h2 = await _run(session_factory, seeded, "normal", {}, script=[("U", "네"), ("B", "좋아요")], continues=cid, extra={"silent_resume": True})
    assert (_started(h2)["fragment_index"], _started(h2)["max_fragments"]) == (2, 3)
    h3 = await _run(session_factory, seeded, "normal", {}, script=[("U", "네"), ("B", "좋아요")], continues=cid, extra={"silent_resume": True})
    assert (_started(h3)["fragment_index"], _started(h3)["max_fragments"]) == (3, 3), "마지막 조각"
    # 상한을 넘긴 4번째는 이어하기 불성립 → 새 통화(조각 1/3) + 선톡 시드(silent 는 무시)
    h4 = await _run(session_factory, seeded, "normal", {}, script=[("B", "안녕!")], continues=cid, extra={"silent_resume": True})
    assert _started(h4)["call_id"] != str(cid) and (_started(h4)["fragment_index"], _started(h4)["max_fragments"]) == (1, 3)
    assert h4["session"].sent_text_turns, "새 통화는 비버가 먼저 인사한다(선톡 시드)"


@pytest.mark.asyncio
async def test_silent_resume_without_a_resumable_call_falls_back_to_the_normal_opening(session_factory, seeded):
    """continues 가 없거나 못 잇는 silent_resume 은 무시 — 새 통화의 선톡 시드가 그대로 나간다(비버가 먼저 인사)."""
    h = await _run(session_factory, seeded, "normal", {}, script=[("B", "안녕!")], extra={"silent_resume": True})
    assert h["session"].sent_text_turns and "[통화 이어감]" not in h["session"].sent_text_turns[0], "선톡 시드(재개 시드 아님)"
    assert reground.RESUME_SILENT_FIRST_ACTION not in h["system_instruction"]
    assert (_started(h)["fragment_index"], _started(h)["max_fragments"]) == (1, 3)
    h2 = await _run(session_factory, seeded, "normal", {}, script=[("B", "안녕!")], continues=999999, extra={"silent_resume": True})
    assert h2["session"].sent_text_turns and reground.RESUME_SILENT_FIRST_ACTION not in h2["system_instruction"]


# --------------------------------------------------------------------------- #
# S2 — 표현학습 조각(cur 경로) silent
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_silent_resume_on_an_expression_call_sends_no_seed_and_appends_the_silent_note(session_factory, seeded):
    h1 = await _run(session_factory, seeded, "expression", {}, script=[("B", "안녕!"), ("U", "네")])
    assert _started(h1)["course"] == "expression" and h1["session"].sent_text_turns
    cid = int(_started(h1)["call_id"])
    h2 = await _run(session_factory, seeded, "auto", {}, script=[("U", "네"), ("B", "좋아요")], continues=cid, extra={"silent_resume": True})
    assert _started(h2)["course"] == "expression" and _started(h2)["call_id"] == str(cid)
    assert h2["session"].sent_text_turns == [], "표현학습 조각2 도 시드 0"
    si = h2["system_instruction"]
    assert "[오늘의 표현" in si and si.rstrip().endswith(seeds.brief_expression_silent_resume("한국어"))
    assert (_started(h2)["fragment_index"], _started(h2)["max_fragments"]) == (2, 3)
    # 회귀 — silent 아님: 종전 seed_expression_resume 1턴 · 쪽지 없음
    h3 = await _run(session_factory, seeded, "auto", {}, script=[("B", "좋아요")], continues=cid)
    assert h3["session"].sent_text_turns[0] == seeds.seed_expression_resume("한국어")
    assert seeds.brief_expression_silent_resume("한국어") not in h3["system_instruction"]


# --------------------------------------------------------------------------- #
# S3 — 무음 시계: 시드 0 조각에서 학습자가 끝내 말하지 않으면 무음 3단으로 끝난다(540s 백스톱에 매달리지 않는다)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_silent_resume_starts_the_idle_clock_at_session_open(session_factory, seeded, monkeypatch):
    """⛔ 통화 시계는 원래 첫 turn_start 에 선다 — 시드 0 이면 학습자가 말할 때까지 turn_start 가 없어 무음 워처가 영영 안 돈다.
    silent 조각은 세션 열기 시각을 기준점으로 삼아 60s/10s/12s(여기선 0.3/0.2/0.2 로 줄임)가 그대로 돈다(사장님 결정 1)."""
    monkeypatch.setattr(cs, "IDLE_NUDGE1_S", 0.3)
    monkeypatch.setattr(cs, "IDLE_NUDGE2_S", 0.2)
    monkeypatch.setattr(cs, "IDLE_CLOSE_S", 0.2)
    h1 = await _run(session_factory, seeded, "normal", {}, script=[("B", "안녕!"), ("U", "네")])
    cid = int(_started(h1)["call_id"])
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    h2 = await _run(session_factory, seeded, "normal", {}, continues=cid, extra={"silent_resume": True}, session_cls=SilentLearnerSession)
    took = loop.time() - t0
    turns = h2["session"].sent_text_turns
    assert len(turns) == 3, (took, turns)
    assert turns[0] == seeds.NUDGE_SEED_1_NORMAL and turns[1] == seeds.NUDGE_SEED_2_NORMAL, "1단·2단 넛지"
    assert "[통화종료" in turns[2], "3단 = 작별 시드 직접 주입"
    assert took < 10, "무음 3단이 돌았다면 1초 안팎이다 — 20s 상한에 걸리면 시계가 안 선 것"


# --------------------------------------------------------------------------- #
# S6 (QA P1-A) — fragment_end 왕복: 저장 뒤 fragment_saved · call_ended 미전송 · turn_index 충돌 0
# --------------------------------------------------------------------------- #
def _frames_of(kind, holder):
    return [f for f in holder["frames"] if f.get("type") == kind]


@pytest.mark.asyncio
async def test_fragment_end_saves_the_fragment_before_fragment_saved_and_sends_no_call_ended(session_factory, seeded, monkeypatch):
    from domains.learning.models.call_raw_data import CallRawData
    order: list[str] = []
    real_persist = cs._persist_remaining

    async def _persist(*a, **k):
        r = await real_persist(*a, **k)
        order.append("persisted")
        return r
    monkeypatch.setattr(cs, "_persist_remaining", _persist)

    def _on_send(text):
        if '"fragment_saved"' in text:
            order.append("fragment_saved")
    h1 = await _run(session_factory, seeded, "normal", {}, script=[("B", "안녕! 오늘 뭐 했어?"), ("U", "학교에 갔어요"), ("B", "좋아요!")],
                    session_cls=HeldOpenSession, fragment_end=True, on_send=_on_send)
    saved = _frames_of("fragment_saved", h1)
    assert saved and saved[0]["call_id"] == _started(h1)["call_id"] and saved[0]["fragment_index"] == 1
    assert not _frames_of("call_ended", h1), "fragment_end 로 끝난 조각은 call_ended 를 보내지 않는다"
    assert h1["frames"][-1]["type"] == "fragment_saved" and h1["ws"].closed_with is not None, "fragment_saved 가 마지막 프레임, 그 뒤 서버가 닫는다"
    assert order == ["persisted", "fragment_saved"], order
    turns = h1["session"].sent_text_turns
    assert turns and all(t == turns[0] and "[통화 시작]" in t for t in turns), ("작별 0·넛지 0 — 선톡 시드(벙어리 재시드 포함) 외 주입 없음", turns)
    cid = int(_started(h1)["call_id"])
    db = session_factory()
    try:
        rows1 = db.query(CallRawData).filter(CallRawData.call_id == cid).count()
        assert rows1 >= 3, "조각1 전사 3턴이 fragment_saved 전에 저장돼 있다"
    finally:
        db.close()
    # 조각2 (silent) — 조각1 꼬리가 이미 저장돼 있으니 turn_index 가 이어지고(충돌 0) 브리프에 직전 교환이 들어간다
    h2 = await _run(session_factory, seeded, "normal", {}, script=[("U", "네"), ("B", "그래서요?")], continues=cid, extra={"silent_resume": True})
    assert (_started(h2)["fragment_index"], _started(h2)["max_fragments"]) == (2, 3)
    assert _frames_of("call_ended", h2) and not _frames_of("fragment_saved", h2), "close 로 끝난 조각은 종전대로 call_ended"
    db = session_factory()
    try:
        rows = db.query(CallRawData.turn_index).filter(CallRawData.call_id == cid).all()
        idx = [r[0] for r in rows]
        assert len(idx) == len(set(idx)) and len(idx) >= rows1 + 2, ("turn_index 충돌", sorted(idx))
    finally:
        db.close()
    assert "[지금까지]" in h2["system_instruction"], "브리프에 직전 조각 맥락이 들어간다"


@pytest.mark.asyncio
async def test_fragment_end_on_an_expression_call_records_progress_before_fragment_saved(session_factory, seeded, monkeypatch):
    from domains.learning.repository import curriculum_repository as repo
    order: list[str] = []
    real_record = cs.cur_svc.record_expression

    def _record(*a, **k):
        r = real_record(*a, **k)
        order.append("recorded")
        return r
    monkeypatch.setattr(cs.cur_svc, "record_expression", _record)

    def _on_send(text):
        if '"fragment_saved"' in text:
            order.append("fragment_saved")
    db = session_factory()
    lesson1 = repo.lesson_by_no(db, "ko", 1)
    surfaces = [it.surface for _li, it in repo.lesson_items(db, lesson1.lesson_id)]
    db.close()
    h1 = await _run(session_factory, seeded, "expression", {},
                    script=[("B", "따라 하세요: «%s»" % surfaces[0]), ("U", surfaces[0]), ("B", "좋아요. «%s»" % surfaces[1])],
                    session_cls=HeldOpenSession, fragment_end=True, on_send=_on_send)
    assert _started(h1)["course"] == "expression" and _frames_of("fragment_saved", h1) and not _frames_of("call_ended", h1)
    assert order == ["recorded", "fragment_saved"], order
    cid = int(_started(h1)["call_id"])
    db = session_factory()
    try:
        cc = repo.cur_call(db, cid)
        assert cc.recorded_fragment == 1 and cc.recorded_at is not None, "조각1 진도가 fragment_saved 전에 커밋됐다"
    finally:
        db.close()
    h2 = await _run(session_factory, seeded, "auto", {}, script=[("U", "네"), ("B", "좋아요")], continues=cid, extra={"silent_resume": True})
    assert _started(h2)["course"] == "expression" and _started(h2)["fragment_index"] == 2
    assert h2["session"].sent_text_turns == []


@pytest.mark.asyncio
async def test_the_old_close_path_still_sends_call_ended(session_factory, seeded):
    h = await _run(session_factory, seeded, "normal", {}, script=[("B", "안녕!"), ("U", "네")])
    assert _frames_of("call_ended", h) and h["frames"][-1]["type"] == "call_ended"
    assert not _frames_of("fragment_saved", h)


@pytest.mark.asyncio
async def test_fragment_end_is_ignored_on_a_level_test():
    st = cs._CallState()
    st.is_leveltest = True
    await cs._handle_client_control(FakeWebSocket([]), json.dumps({"type": "fragment_end"}), st)   # raise 없음
    st2 = cs._CallState()
    with pytest.raises(cs._FragmentEnd):
        await cs._handle_client_control(FakeWebSocket([]), json.dumps({"type": "fragment_end"}), st2)
    assert cs._CALL_SIGNALS == (cs._CallFinished, cs._ClientDisconnect, cs._FragmentEnd), "종료 > 클라 끊김 > 조각 끝"


# --------------------------------------------------------------------------- #
# S7 (QA P1-B) — start 창: 바이너리는 세지 않는다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_initial_start_window_ignores_binary_frames(session_factory, seeded):
    h1 = await _run(session_factory, seeded, "normal", {}, script=[("B", "안녕!"), ("U", "네")])
    cid = int(_started(h1)["call_id"])
    mic = [{"type": "websocket.receive", "bytes": bytes(640)} for _ in range(8)]     # 재연결 소켓에 마이크 8프레임이 start 보다 먼저
    h2 = await _run(session_factory, seeded, "normal", {}, script=[("U", "네"), ("B", "좋아요")], continues=cid,
                    extra={"silent_resume": True}, before_start=mic)
    assert _started(h2)["call_id"] == str(cid), "start 가 9번째 메시지여도 읽는다 — 이어하기 유실 0"
    assert (_started(h2)["fragment_index"], _started(h2)["max_fragments"]) == (2, 3)
    assert h2["session"].sent_text_turns == []


# --------------------------------------------------------------------------- #
# S8 (QA P2) — T22 2세대 브리프: silent 조각에서 학습자 첫 발화 전이면 «기다려라»
# --------------------------------------------------------------------------- #
def test_reconnect_brief_waits_in_a_silent_fragment():
    from core.prompts.locked.rules import CONTROL_TAG
    plain_head = (f"{CONTROL_TAG} 연결이 잠깐 끊겼다가 이어졌다. 끊긴 것을 사과하지 말고, 인사도 다시 하지 말고, "
                  "하던 것을 그대로 이어가라. ")
    st = cs._CallState()
    assert cs._reconnect_brief(st) == plain_head, "종전 경로 바이트 불변(세그먼트 없음 → head 만)"
    st.silent_resume = True
    b = cs._reconnect_brief(st)
    assert b != plain_head and "먼저 말을 꺼내지 마라 — 학습자가 먼저 말한다" in b and "사과하지 말고" in b
    st.learner_spoke = True
    assert cs._reconnect_brief(st) == plain_head, "학습자가 이미 말한 뒤에는 종전 head"
