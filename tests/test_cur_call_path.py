"""커리큘럼 2단계 B2 — 통화 경로 배선 (run_call + 가짜 Live 세션 + sqlite 실제 시드).

계획 docs/plans/2026-09-12-cur-2단계-통화경로-이전.md §2·§6 ②③·§7 P0·P1-4·ⓑ·§8. 잠그는 것:
  auto → 서버가 코스 결정·call_started.course · COURSE_LOCKED 프레임 + 소켓 닫힘(call 행 failed) · CUR_ENABLED=false 바이트 동일 ·
  cur 경로에서 save_expression_progress / promote_by_expression 0회 · 조각2(continues_call_id) 재개 시 cur_call INSERT 0 ·
  종료 저장이 cur_member_item 에 · 프리토킹 lesson=None 스냅샷 · 시드 없으면 옛 경로 폴백(R5).
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
from domains.learning.service import curriculum_service as cur
from domains.learning.repository import curriculum_repository as repo
from core.config import settings as app_settings
from core.gemini_live import LiveEvent
from core.prompts.freetalk import build_freetalk_instruction
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
        m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id=f"auth-cur-path-{_n['i']}")
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
    # ⚠ REGROUND_MODE 를 off 로 두지 않는다 — state.reground_items(covered 검출의 라벨 목록)가 그 분기 안에서 채워진다
    #   (옛 경로와 같은 결합 — B2 보고에 적음).


class FakeWebSocket:
    def __init__(self, incoming: list[dict]):
        self._incoming = list(incoming)
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closed_with: int | None = None
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
        self.closed_with = code
        self.client_state = self._WS.DISCONNECTED


class FakeLiveSession:
    """비버가 표현 몇 개를 말한다(→ covered) — script 는 (role, text) 목록."""

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


def _factory(holder, script=None):
    @contextlib.asynccontextmanager
    async def _f(client, settings, *, system_instruction, voice, **_kw):
        sess = FakeLiveSession(script or [("B", "안녕!")])
        holder["session"] = sess
        holder["system_instruction"] = system_instruction
        yield sess
    return _f


async def _run(session_factory, seeded, call_type, holder, *, script=None, continues=None, extra=None):
    start = {"type": "start", "character_id": seeded["character_id"], **(extra or {})}
    if call_type is not None:
        start["call_type"] = call_type
    if continues is not None:
        start["continues_call_id"] = str(continues)
    ws = FakeWebSocket([{"type": "websocket.receive", "text": json.dumps(start)}])
    await run_call(ws, app_settings, object(), session_factory,
                   member_id=seeded["member_id"], live_session_factory=_factory(holder, script))
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


# --------------------------------------------------------------------------- #
# auto → 코스 결정 · call_started.course · cur_call INSERT · 저장은 cur_member_item 에
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_auto_opens_an_expression_call_on_lesson_1_and_records_to_cur(session_factory, seeded, monkeypatch):
    calls = {"old_save": 0, "old_promote": 0}
    monkeypatch.setattr(svc, "save_expression_progress", lambda *a, **k: calls.__setitem__("old_save", calls["old_save"] + 1))
    monkeypatch.setattr(cs.svc, "save_expression_progress", svc.save_expression_progress)
    from domains.learning.service import mastery_service
    monkeypatch.setattr(mastery_service, "promote_by_expression", lambda *a, **k: calls.__setitem__("old_promote", 1))
    db = session_factory()
    lesson1 = repo.lesson_by_no(db, "ko", 1)
    surfaces = [it.surface for _li, it in repo.lesson_items(db, lesson1.lesson_id)]
    db.close()
    h = await _run(session_factory, seeded, "auto", {}, script=[("B", f'따라 하세요: "{surfaces[0]}"'), ("U", surfaces[1]), ("B", f'"{surfaces[2]}"')])
    assert _started(h)["course"] == "expression"
    assert "[오늘의 표현" in h["system_instruction"] and surfaces[0] in h["system_instruction"]
    db = session_factory()
    try:
        call = _last_call(db, seeded["member_id"])
        assert call.call_type == "expression"
        cc = repo.cur_call(db, call.call_id)
        assert cc is not None and cc.course == "expression" and cc.lesson_id == lesson1.lesson_id
        assert cc.recorded_at is not None, "종료 저장이 cur 경로로 찍혔다"
        mine = repo.member_item_map(db, seeded["member_id"], lesson1.lesson_id)
        drilled = {iid for iid, r in mine.items() if r.drilled_at is not None}
        assert len(drilled) == 3, "비버 2 + 학습자 1 = covered 3 → drilled 3"
        assert all(r.seen_count == 1 for r in mine.values()) and len(mine) == 15, "목록 전부 seen_count+1"
        rows = json.loads(cc.items)
        assert {r["item_id"] for r in rows} == drilled and all("review" in r for r in rows)
        # 운영 1440 실측 — role·drilled 가 None/false 로 저장되던 것. 8키 전부 실값(cur_call.items 계약, 하네스가 읽는다)
        for r in rows:
            assert set(r) == {"item_id", "role", "surface", "meaning", "drilled", "passed", "failed", "review"}, r
            assert r["role"] == "chunk" and r["drilled"] is True and r["review"] is False, r
            assert r["surface"] and r["passed"] is False and r["failed"] is False
        assert call.expression_result is None, "옛 결과 컬럼은 쓰지 않는다"
    finally:
        db.close()
    assert calls == {"old_save": 0, "old_promote": 0}, "cur 경로에서 옛 저장·승급이 불렸다"


@pytest.mark.asyncio
async def test_explicit_expression_and_freetalk_go_through_cur_and_freetalk_is_locked_first(session_factory, seeded):
    h = await _run(session_factory, seeded, "freetalk", {})
    err = next(f for f in h["frames"] if f.get("type") == "error")
    assert err["code"] == "COURSE_LOCKED" and err["recoverable"] is False and "L1-S01-1" in err["message"]
    assert not any(f.get("type") == "call_started" for f in h["frames"]), "잠기면 call_started 전에 끊는다"
    assert h["ws"].closed_with == 1008
    db = session_factory()
    try:
        call = _last_call(db, seeded["member_id"])
        assert call.status == "failed" and repo.cur_call(db, call.call_id) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_freetalk_opens_after_expression_done_with_lesson_block_and_completes(session_factory, seeded, monkeypatch):
    db = session_factory()
    m = seeded["member_id"]
    lesson1 = repo.lesson_by_no(db, "ko", 1)
    surfaces = [it.surface for _li, it in repo.lesson_items(db, lesson1.lesson_id)]
    db.close()
    # 표현학습 한 통화로 15개 전부 드릴 → expression_done
    h = await _run(session_factory, seeded, "auto", {}, script=[("B", f'"{s}"') for s in surfaces])
    db = session_factory()
    assert repo.lesson_status(db, m, lesson1.lesson_id).status == "expression_done"
    db.close()
    # auto → 프리토킹, 지시문에 [이번 차시] 블록
    monkeypatch.setattr(cs, "SEED_TO_HANGUP_S", 0.2)
    h2 = await _run(session_factory, seeded, "auto", {}, script=[("B", "오늘은 자유롭게 이야기해요."), ("B", "네, 좋아요.")])
    assert _started(h2)["course"] == "freetalk"
    si = h2["system_instruction"]
    # 프리토킹 v1 차시판(2026-09-12-프리토킹-코스-대본 §3·§9) — 과제 대본 + [이번 차시] 소재 전부(청크 15) + 문장 수 2 + 차시판 선톡
    assert "[이번 차시 — 이 상황을 역할극으로 대화한다]" in si and lesson1.situation in si
    assert "**역할극**이다" in si and "**그 턴만 선생님으로 돌아와**" in si and "다음 턴부터는 다시 그 인물로, 전부 한국어다" in si
    assert "**네가 이 사람이다.**" in si and "과제" not in si
    assert all(s in si for s in surfaces) and "[학습자 흥미" not in si and "1~2문장" in si
    assert h2["session"].sent_text_turns[0].startswith("[통화 시작]") and "«상대» 인물로서 첫 말을 건다" in h2["session"].sent_text_turns[0]
    db = session_factory()
    try:
        call = _last_call(db, m)
        assert call.call_type == "freetalk" and repo.cur_call(db, call.call_id).course == "freetalk"
        # 가짜 통화는 60초 미만 → freetalk_done 안 됨(§7 ⓑ) — 포인터 유지
        assert repo.lesson_status(db, m, lesson1.lesson_id).status == "expression_done"
        assert repo.current_progress(db, m).lesson_id == lesson1.lesson_id
    finally:
        db.close()


@pytest.mark.asyncio
async def test_fragment_resume_does_not_insert_a_second_cur_call(session_factory, seeded, monkeypatch):
    monkeypatch.setattr(cs.call_service, "call_fragments_for_member", lambda db, m: 3)   # Free 는 조각 1 — 이어하기 관문을 연다
    h = await _run(session_factory, seeded, "expression", {}, script=[("B", "안녕!")])
    db = session_factory()
    call = _last_call(db, seeded["member_id"])
    n_before = db.execute(text("SELECT COUNT(*) FROM cur_call")).scalar()
    # 이어하기 관문(TTL 5분) 안이라 같은 call 로 조각2
    db.close()
    h2 = await _run(session_factory, seeded, "auto", {}, script=[("B", "이어서 할게요.")], continues=call.call_id)
    assert _started(h2)["call_id"] == str(call.call_id) and _started(h2)["course"] == "expression"
    db = session_factory()
    try:
        assert db.execute(text("SELECT COUNT(*) FROM cur_call")).scalar() == n_before, "조각2 는 cur_call INSERT 0"
        assert db.query(Call).filter(Call.member_id == seeded["member_id"]).count() == 1
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# admin QA 우회 — force_course: 잠금을 건너 지금 차시 프리토킹, 진도 무영향. user·force 없음은 잠금 그대로.
# --------------------------------------------------------------------------- #
def _set_role(session_factory, member_id, role):
    db = session_factory()
    db.get(Member, member_id).role = role
    db.commit(); db.close()


def _lesson_state(session_factory, member_id):
    db = session_factory()
    try:
        prog = repo.current_progress(db, member_id)
        ml = repo.lesson_status(db, member_id, prog.lesson_id) if prog is not None else None
        return (prog.lesson_id if prog else None, ml.status if ml is not None else None)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_admin_force_course_opens_freetalk_on_the_locked_lesson_without_touching_progress(session_factory, seeded, monkeypatch):
    monkeypatch.setattr(cs, "SEED_TO_HANGUP_S", 0.2)
    monkeypatch.setattr(cs.call_service, "call_fragments_for_member", lambda db, m: 3)
    m = seeded["member_id"]
    _set_role(session_factory, m, "admin")
    db = session_factory(); lesson1 = repo.lesson_by_no(db, "ko", 1); db.close()
    # 표현학습 0 → 잠금 상태인데 admin+force 로 열린다
    calls = {"complete": 0}
    real_complete = cur.complete_freetalk
    monkeypatch.setattr(cs.cur_svc, "complete_freetalk", lambda *a, **k: (calls.__setitem__("complete", calls["complete"] + 1), real_complete(*a, **k))[1])
    h = await _run(session_factory, seeded, "freetalk", {}, script=[("B", "안녕하세요? 이름이 뭐예요?"), ("U", "저는 존이에요.")], extra={"force_course": True})
    assert _started(h)["course"] == "freetalk" and not any(f.get("type") == "error" for f in h["frames"])
    assert "[이번 차시 — 이 상황을 역할극으로 대화한다]" in h["system_instruction"] and lesson1.situation in h["system_instruction"]
    db = session_factory()
    try:
        call = _last_call(db, m)
        cc = repo.cur_call(db, call.call_id)
        assert cc is not None and cc.course == "freetalk" and cc.lesson_id == lesson1.lesson_id, "cur_call 은 평소처럼 INSERT(경로 고정·결과 화면)"
    finally:
        db.close()
    assert calls["complete"] == 0, "강제 통화는 완료 훅 0"
    assert _lesson_state(session_factory, m) == (lesson1.lesson_id, None), "status·포인터 그대로(표현학습 기록 없음 → 행 없음)"
    # 조각 재개(continues_call_id) 도 강제 — 완료 훅 0 · cur_call INSERT 0
    db = session_factory(); n_before = db.execute(text("SELECT COUNT(*) FROM cur_call")).scalar(); db.close()
    h2 = await _run(session_factory, seeded, "freetalk", {}, script=[("B", "이어서 할게요.")], continues=call.call_id)
    assert _started(h2)["call_id"] == str(call.call_id) and _started(h2)["course"] == "freetalk"
    assert calls["complete"] == 0
    db = session_factory()
    try:
        assert db.execute(text("SELECT COUNT(*) FROM cur_call")).scalar() == n_before
    finally:
        db.close()
    assert _lesson_state(session_factory, m) == (lesson1.lesson_id, None)


@pytest.mark.asyncio
async def test_force_course_is_ignored_for_a_user_and_absent_force_keeps_the_lock_for_an_admin(session_factory, seeded):
    m = seeded["member_id"]
    # user + force → 잠금 그대로(조용히 무시)
    h = await _run(session_factory, seeded, "freetalk", {}, extra={"force_course": True})
    err = next(f for f in h["frames"] if f.get("type") == "error")
    assert err["code"] == "COURSE_LOCKED" and h["ws"].closed_with == 1008
    assert not any(f.get("type") == "call_started" for f in h["frames"])
    # admin, force 없음 → 잠금 그대로
    _set_role(session_factory, m, "admin")
    h2 = await _run(session_factory, seeded, "freetalk", {})
    assert next(f for f in h2["frames"] if f.get("type") == "error")["code"] == "COURSE_LOCKED"
    # admin + force 이지만 expression/auto 엔 영향 없다 — 표현학습이 열린다
    h3 = await _run(session_factory, seeded, "auto", {}, script=[("B", "안녕!")], extra={"force_course": True})
    assert _started(h3)["course"] == "expression"
    db = session_factory()
    try:
        assert db.query(Call).filter(Call.member_id == m, Call.status == "failed").count() == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cur_disabled_keeps_the_old_path_byte_identical(session_factory, seeded, monkeypatch):
    monkeypatch.setattr(app_settings, "CUR_ENABLED", False)
    h = await _run(session_factory, seeded, "freetalk", {})
    started = _started(h)
    assert "course" not in started, "옛 경로 프레임은 course 키 자체가 없다(바이트 동일)"
    assert "[이번 차시" not in h["system_instruction"]
    db = session_factory()
    try:
        call = _last_call(db, seeded["member_id"])
        assert repo.cur_call(db, call.call_id) is None and call.call_type == "freetalk"
    finally:
        db.close()
    # auto 인데 스위치가 꺼지면 옛 표현학습 경로로 떨어진다(죽지 않게)
    h2 = await _run(session_factory, seeded, "auto", {})
    db = session_factory()
    try:
        assert _last_call(db, seeded["member_id"]).call_type == "expression"
    finally:
        db.close()


def test_freetalk_prompt_without_lesson_is_byte_identical():
    base = dict(role="비버", personality="다정", level_profile="쉬운 문장", locale="en", interests=["축구"], name="T", target_language="한국어")
    assert build_freetalk_instruction(**base) == build_freetalk_instruction(**base, lesson=None)
    brief = cur.CurFreetalkBrief(situation="가게에서 부탁하기", partner="점원", surfaces=["이거 주세요", "얼마예요?"], probes=["뭐 사고 싶어요?"])
    with_block = build_freetalk_instruction(**base, lesson=brief)
    # 차시판은 **다른 대본**이다(규칙 1·3·4 코스판, 흥미 블록 없음) — 옛 출력의 접두가 아니다. 호환: items 없는 브리프는 surfaces 를 «표현» 줄로
    assert "[이번 차시 — 이 상황을 역할극으로 대화한다]" in with_block and "표현: 이거 주세요 · 얼마예요?" in with_block
    assert "점원" in with_block and "뭐 사고 싶어요?" in with_block and "[학습자 흥미" not in with_block


def test_protocol_start_accepts_auto_and_started_carries_course():
    from domains.learning.realtime.protocol import ClientStart, ServerCallStarted
    assert ClientStart(type="start", character_id=1, call_type="auto").call_type == "auto"
    plain = ServerCallStarted(character_id=1, call_id="7").model_dump_json(exclude_none=True)
    assert "course" not in plain
    assert '"course":"freetalk"' in ServerCallStarted(character_id=1, call_id="7", course="freetalk").model_dump_json(exclude_none=True)
