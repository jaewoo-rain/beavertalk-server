"""Q5(2026-09-24, 프론트 실기기 QA) — 예산 집계를 클라가 직접 무력화하는 경로 둘을 막는다.

결함 ①: `RawDataIn.total_time`·`CallCreate.total_time` 에 하한이 없어, `POST /api/v1/calls`
에 음수 `total_time` 한 건이면 하루 예산 SUM(call_repository.sum_total_time_in_window)
이 음수로 내려가 그날 상한이 사실상 소멸한다(매일 반복 가능).
결함 ②: `DELETE /api/v1/calls/{id}` 가 하드 삭제라, 삭제된 통화의 `total_time` 이 SUM 에서
빠져 예산이 되돌아온다(반복 삭제로 무한 리필).

수정: ① `total_time` 에 `ge=0`(0은 정상값이라 허용). ② `DELETE /calls/{id}` 를
admin 전용(`CurrentAdmin`)으로 좁혔다 — 앱이 이 호출을 아예 안 쓴다(Flutter 전수 검색
0건). admin 은 `is_unlimited_member` 로 애초에 예산 대상이 아니고, 삭제도 자기 소유
통화만 가능해(`_assert_owner`) 다른 회원 예산을 되돌릴 경로가 없다.

근거: docs/plans/2026-09-22-프리미엄-자유대화-15분-달력.md(C4, 예산)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.deps as deps
from core.config import settings as app_settings
from core.supabase_auth import AuthUser
from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.repository.call_repository import CallRepository
from domains.learning.schemas.call import CallCreate, RawDataIn


def _fake_verify(token):
    if token and token.startswith("auth-"):
        return AuthUser(uid=token, email=f"{token}@test.io")
    return None


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(deps, "verify_token", _fake_verify)


@pytest.fixture()
def session_factory():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _seed(session_factory, *, role="user"):
    db = session_factory()
    try:
        voice = Voice(name="V", gender="male"); db.add(voice); db.flush()
        ch = Character(name="바바", role="선생님", personality="시크",
                       voice_id=voice.voice_id, price=0)
        db.add(ch); db.flush()
        member = Member(language="en", korean_level=1, onboarding_completed=True,
                        auth_user_id="auth-member", role=role)
        db.add(member); db.flush()
        call = Call(member_id=member.member_id, character_id=ch.character_id,
                   status="done", call_type="chat", total_time=100,
                   call_date=datetime.now(timezone.utc))
        db.add(call); db.commit()
        return {"member_id": member.member_id, "call_id": call.call_id, "ch_id": ch.character_id}
    finally:
        db.close()


def _build_app(session_factory):
    from main import create_app
    app = create_app()
    app.state.session_factory = session_factory
    app.state.settings = app_settings
    app.state.genai_client = object()
    return app


def _hdr(auth="auth-member"):
    return {"Authorization": f"Bearer {auth}"}


# --------------------------------------------------------------------------- #
# 1) DTO 하한 — 음수 total_time 거부, 0 허용
# --------------------------------------------------------------------------- #
def test_call_create_rejects_negative_total_time():
    with pytest.raises(ValidationError):
        CallCreate(character_id=1, total_time=-100000)


def test_call_create_allows_zero_total_time():
    dto = CallCreate(character_id=1, total_time=0)
    assert dto.total_time == 0


def test_raw_data_in_rejects_negative_total_time():
    with pytest.raises(ValidationError):
        RawDataIn(total_time=-1)


def test_raw_data_in_allows_zero_total_time():
    dto = RawDataIn(total_time=0)
    assert dto.total_time == 0


def test_post_calls_with_negative_total_time_is_422(session_factory):
    seeded = _seed(session_factory)
    client = TestClient(_build_app(session_factory))
    r = client.post(
        "/api/v1/calls",
        json={"character_id": seeded["ch_id"], "total_time": -100000},
        headers=_hdr(),
    )
    assert r.status_code == 422, r.text


def test_post_calls_with_zero_total_time_is_created(session_factory):
    seeded = _seed(session_factory)
    client = TestClient(_build_app(session_factory))
    r = client.post(
        "/api/v1/calls",
        json={"character_id": seeded["ch_id"], "total_time": 0},
        headers=_hdr(),
    )
    assert r.status_code == 201, r.text
    assert r.json()["total_time"] == 0


# --------------------------------------------------------------------------- #
# 2) DELETE /calls/{id} — admin 전용, 삭제 뒤에도 예산 사용량이 줄지 않는다(비admin 경로)
# --------------------------------------------------------------------------- #
def test_delete_call_is_forbidden_for_a_non_admin_member(session_factory):
    seeded = _seed(session_factory, role="user")
    client = TestClient(_build_app(session_factory))
    r = client.delete(f"/api/v1/calls/{seeded['call_id']}", headers=_hdr())
    assert r.status_code == 403, r.text


def test_non_admin_cannot_shrink_budget_usage_via_delete(session_factory):
    """⛔⛔ 핵심 — 일반 회원은 이제 삭제 자체가 막히므로(403), 삭제 전후로 예산
    사용량(SUM(total_time))이 그대로여야 한다."""
    seeded = _seed(session_factory, role="user")
    client = TestClient(_build_app(session_factory))

    from datetime import timedelta

    db = session_factory()
    try:
        window = (datetime.now(timezone.utc) - timedelta(days=1),
                  datetime.now(timezone.utc) + timedelta(days=1))
        before = CallRepository(db).sum_total_time_in_window(seeded["member_id"], *window)
    finally:
        db.close()
    assert before == 100

    r = client.delete(f"/api/v1/calls/{seeded['call_id']}", headers=_hdr())
    assert r.status_code == 403

    db = session_factory()
    try:
        after = CallRepository(db).sum_total_time_in_window(seeded["member_id"], *window)
    finally:
        db.close()
    assert after == before == 100, "삭제가 거절됐는데도 예산 사용량이 줄었다"


def test_delete_call_succeeds_for_an_admin_member(session_factory):
    seeded = _seed(session_factory, role="admin")
    client = TestClient(_build_app(session_factory))
    r = client.delete(f"/api/v1/calls/{seeded['call_id']}", headers=_hdr())
    assert r.status_code == 204, r.text

    db = session_factory()
    try:
        assert db.get(Call, seeded["call_id"]) is None
    finally:
        db.close()
