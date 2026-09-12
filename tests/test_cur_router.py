"""cur_* 2단계 B3 — 조회 라우터(GET /api/v1/cur/me · /api/v1/cur/lessons) · 결과 화면 폴백 · dev 초기화(POST /__dev/cur-reset).

TestClient + sqlite 인메모리 + **실제 시드 7MB**(test_cur_selection.py 관례). 인증은 Bearer 토큰 == auth uuid 스텁.
계획 docs/plans/2026-09-12-cur-2단계-통화경로-이전.md §2·§8.
"""
from __future__ import annotations

import io
import json
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Integer, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import core.deps as deps
from core.config import settings as app_settings
from core.supabase_auth import AuthUser
from db.registry import Base
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.curriculum import CurCall
from domains.learning.models.learning_item import LearningItem
from domains.learning.repository import curriculum_repository as repo
from domains.learning.service import curriculum_service as cur
from scripts.curriculum.load_cur_seed import load

SEED = os.path.join(os.path.dirname(__file__), "..", "assets", "curriculum_v3", "cur_seed.json")
pytestmark = pytest.mark.skipif(not os.path.exists(SEED), reason="cur_seed.json 없음")


def _fake_verify(token):
    if token and token.startswith("auth-"):
        return AuthUser(uid=token, email=f"{token}@test.io")
    return None


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(deps, "verify_token", _fake_verify)


@pytest.fixture(scope="module")
def factory():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            if len(t.primary_key.columns) == 1:
                pk.type = Integer()
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sf() as s:
        for i in range(46):   # 시드 적재기가 옛 테이블에서 청크 46개를 복사한다(결정 #13) — 시험용 더미
            s.add(LearningItem(
                language="ko", kind="chunk", source_key=f"c:{i}", band=1, level_no=1, assign_rule="seed",
                surface=f"청크 문장 {i}", meanings=json.dumps({"en": f"chunk {i}"}), examples="[]",
            ))
        s.commit()
        load(s, json.load(io.open(SEED, encoding="utf-8")), dry_run=False)
        s.commit()
        v = Voice(name="Fenrir", gender="male")
        s.add(v); s.flush()
        s.add(Character(name="비비", role="선생님", personality="다정함", voice_id=v.voice_id, price=0))
        s.commit()
    return sf


@pytest.fixture()
def client(factory):
    from main import create_app
    app = create_app()
    app.state.session_factory = factory
    app.state.settings = app_settings
    app.state.genai_client = object()
    return TestClient(app)


@pytest.fixture()
def db(factory):
    s = factory()
    yield s
    s.rollback()
    s.close()


_counter = {"n": 0}


def _member(db: Session, *, role: str = "member") -> tuple[int, dict]:
    _counter["n"] += 1
    auth = f"auth-cur-r{_counter['n']}"
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id=auth, role=role)
    db.add(m); db.commit()
    return m.member_id, {"Authorization": f"Bearer {auth}"}


def _call(db: Session, member_id: int, **kw) -> int:
    ch = db.execute(text("SELECT character_id FROM character LIMIT 1")).scalar()
    c = Call(member_id=member_id, character_id=ch, call_type="expression", call_date=datetime.now(timezone.utc),
             status="done", **kw)
    db.add(c); db.commit()
    return c.call_id


# --------------------------------------------------------------------------- #
# GET /api/v1/cur/me
# --------------------------------------------------------------------------- #
def test_me_for_a_new_member_is_lesson_1_learning_with_expression_open_and_freetalk_locked(client, db):
    _, hdr = _member(db)
    r = client.get("/api/v1/cur/me", headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lesson"]["no"] == 1 and body["lesson"]["code"] == "L1-S01-1" and body["lesson"]["level_no"] == 1
    assert body["status"] == "learning"
    assert body["items_total"] == 15 and body["items_drilled"] == 0
    assert body["open"] == {"expression": True, "freetalk": False}
    assert set(body) == {"lesson", "status", "items_total", "items_drilled", "open", "next_course"}
    assert body["next_course"] == "expression"
    assert set(body["lesson"]) == {"no", "code", "level_no", "situation", "topic"}
    # 두 번 불러도 같은 답(멱등 — 포인터를 한 번만 만든다)
    assert client.get("/api/v1/cur/me", headers=hdr).json() == body


def test_me_requires_auth(client):
    assert client.get("/api/v1/cur/me").status_code == 401


def test_me_reflects_progress_after_a_recorded_expression_call(client, db):
    m, hdr = _member(db)
    c = _call(db, m)
    opened = cur.open_call(db, m, c, "expression")
    cur.record_expression(db, c, opened.items, drilled_ids=[d["item_id"] for d in opened.items],
                          passed_ids=[d["item_id"] for d in opened.items], failed_ids=[])
    body = client.get("/api/v1/cur/me", headers=hdr).json()
    assert body["lesson"]["no"] == 1
    assert body["status"] == "expression_done"
    assert body["items_drilled"] == 15
    assert body["open"] == {"expression": True, "freetalk": True}
    assert body["next_course"] == "freetalk", "auto 로 걸면 프리토킹 — 홈 카드가 이 값을 보인다"


# --------------------------------------------------------------------------- #
# GET /api/v1/cur/lessons
# --------------------------------------------------------------------------- #
def test_lessons_level_filter_returns_only_that_level_in_no_order_with_null_status(client, db):
    _, hdr = _member(db)
    r = client.get("/api/v1/cur/lessons", params={"level": 2}, headers=hdr)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert rows and all(x["level_no"] == 2 for x in rows)
    assert [x["no"] for x in rows] == sorted(x["no"] for x in rows)
    assert all(x["status"] is None for x in rows)            # 아직 안 시작한 레벨
    assert set(rows[0]) == {"no", "code", "level_no", "situation", "status"}


def test_lessons_without_filter_marks_the_current_pointer_as_learning(client, db):
    _, hdr = _member(db)
    client.get("/api/v1/cur/me", headers=hdr)                 # 포인터 생성
    rows = client.get("/api/v1/cur/lessons", headers=hdr).json()
    assert rows[0]["no"] == 1 and rows[0]["status"] == "learning"
    assert all(x["status"] is None for x in rows[1:])
    assert len({x["level_no"] for x in rows}) > 1             # 전 레벨


def test_lessons_rejects_level_below_1(client, db):
    _, hdr = _member(db)
    assert client.get("/api/v1/cur/lessons", params={"level": 0}, headers=hdr).status_code == 422


# --------------------------------------------------------------------------- #
# GET /api/v1/calls/{id}/result — quiz_items: cur_call.items 우선, 없으면 옛 스냅샷
# --------------------------------------------------------------------------- #
def test_call_result_reads_cur_call_items_first_with_review_flag(client, db):
    m, hdr = _member(db)
    c = _call(db, m)
    lesson = repo.lesson_by_no(db, "ko", 1)
    rows = [
        {"item_id": 11, "role": "chunk", "surface": "안녕하세요", "meaning": "hello", "drilled": True, "passed": True, "failed": False, "review": False},
        {"item_id": 12, "role": "chunk", "surface": "감사합니다", "meaning": "thanks", "drilled": True, "passed": False, "failed": True, "review": True},
    ]
    db.add(CurCall(call_id=c, lesson_id=lesson.lesson_id, course="expression", items=json.dumps(rows, ensure_ascii=False)))
    db.commit()
    r = client.get(f"/api/v1/calls/{c}/result", headers=hdr)
    assert r.status_code == 200, r.text
    q = r.json()["quiz_items"]
    assert q == [
        {"item_id": 11, "surface": "안녕하세요", "meaning": "hello", "passed": True, "failed": False, "review": False},
        {"item_id": 12, "surface": "감사합니다", "meaning": "thanks", "passed": False, "failed": True, "review": True},
    ]


def test_call_result_falls_back_to_old_snapshot_when_no_cur_call(client, db):
    m, hdr = _member(db)
    old = [{"item_id": 7, "surface": "물", "meaning": "water", "passed": False, "failed": True}]
    c = _call(db, m, expression_result=json.dumps(old, ensure_ascii=False))
    q = client.get(f"/api/v1/calls/{c}/result", headers=hdr).json()["quiz_items"]
    assert q == [{"item_id": 7, "surface": "물", "meaning": "water", "passed": False, "failed": True, "review": False}]


def test_call_result_with_neither_is_empty(client, db):
    m, hdr = _member(db)
    c = _call(db, m)
    assert client.get(f"/api/v1/calls/{c}/result", headers=hdr).json()["quiz_items"] == []


# --------------------------------------------------------------------------- #
# POST /__dev/cur-reset
# --------------------------------------------------------------------------- #
def test_cur_reset_wipes_my_state_and_points_at_lesson_no(client, db):
    m, hdr = _member(db, role="admin")
    c = _call(db, m)
    opened = cur.open_call(db, m, c, "expression")
    cur.record_expression(db, c, opened.items, drilled_ids=[d["item_id"] for d in opened.items],
                          passed_ids=[d["item_id"] for d in opened.items], failed_ids=[])
    assert client.get("/api/v1/cur/me", headers=hdr).json()["status"] == "expression_done"

    r = client.post("/__dev/cur-reset", json={}, headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["member_id"] == m and r.json()["lesson"] == {"no": 1, "code": "L1-S01-1"} and r.json()["deleted_calls"] == 1
    me = client.get("/api/v1/cur/me", headers=hdr).json()
    assert me["status"] == "learning" and me["items_drilled"] == 0 and me["open"]["freetalk"] is False
    assert repo.cur_call(db, c) is None
    assert db.get(Call, c) is not None                        # call 행은 보존

    r = client.post("/__dev/cur-reset", json={"lesson_no": 3}, headers=hdr)
    assert r.status_code == 200 and r.json()["lesson"]["no"] == 3
    assert client.get("/api/v1/cur/me", headers=hdr).json()["lesson"]["no"] == 3


def test_cur_reset_targets_another_member_and_rejects_unknown_lesson(client, db):
    admin_id, admin_hdr = _member(db, role="admin")
    other, other_hdr = _member(db)
    client.get("/api/v1/cur/me", headers=other_hdr)
    r = client.post("/__dev/cur-reset", json={"member_id": other, "lesson_no": 2}, headers=admin_hdr)
    assert r.status_code == 200 and r.json()["member_id"] == other and r.json()["lesson"]["no"] == 2
    assert client.get("/api/v1/cur/me", headers=other_hdr).json()["lesson"]["no"] == 2
    assert client.get("/api/v1/cur/me", headers=admin_hdr).json()["lesson"]["no"] == 1   # 관리자 본인은 그대로
    assert client.post("/__dev/cur-reset", json={"lesson_no": 99999}, headers=admin_hdr).status_code == 404


def test_cur_reset_is_admin_only(client, db):
    _, hdr = _member(db)
    assert client.post("/__dev/cur-reset", json={}, headers=hdr).status_code == 403


def test_cur_router_and_schema_do_not_name_old_learning_tables():
    root = os.path.join(os.path.dirname(__file__), "..")
    for rel in ("domains/learning/routers/curriculum.py", "domains/learning/schemas/curriculum.py"):
        src = open(os.path.join(root, rel), encoding="utf-8").read()
        for banned in ("learning_item", "member_item_progress", "korean_level", "expression_result", "item_evidence"):
            assert banned not in src, f"{rel}: {banned}"
