"""§17 해지 사유 수집 회귀 — POST /members/me/churn-reasons (외부 의존 0).

검증 대상 (docs/plans/2026-09-28-알람시간대-해지사유-5-17.md §17):
    - 유니크 키 (member_id, subscribe_id) — 앱이 보낸 subscribe_id 소유만 검증한다
      (서버가 resolve_status 로 "지금 만료된 구독"을 스스로 추론하지 않는다).
    - 남의 subscribe_id / 없는 subscribe_id → 404(존재를 알리지 않는다).
    - 재제출(같은 회원×구독)은 마지막 값으로 덮어쓴다(행 1개 유지).
    - offer_shown 은 서버가 추론하지 않고 앱이 보낸 값 그대로 저장한다.
    - end_date_snapshot 은 응답 시점 그 구독의 end_date 를 스냅샷한다.
    - reason 은 5종 CHECK(DB 레벨) — 잘못된 값은 커밋 시 거부된다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.churn_reason import ChurnReason
from domains.commerce.models.subscribe import Subscribe
from domains.commerce.schemas.churn_reason import ChurnReasonIn
from domains.commerce.service.churn_reason_service import ChurnReasonService
from fastapi import HTTPException

import core.deps as deps
from core.supabase_auth import AuthUser


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
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _member(db, auth_user_id="auth-a"):
    m = Member(language="en", korean_level=3, onboarding_completed=True,
               auth_user_id=auth_user_id)
    db.add(m)
    db.commit()
    return m


def _subscribe(db, member_id, end_date=None):
    sub = Subscribe(member_id=member_id, end_date=end_date, is_activate=False)
    db.add(sub)
    db.commit()
    return sub


# --------------------------------------------------------------------------- #
# 1) 서비스 계층 — 소유 검증·upsert·스냅샷
# --------------------------------------------------------------------------- #
def test_submit_creates_row_and_snapshots_end_date(db):
    m = _member(db)
    end = datetime.now(timezone.utc) - timedelta(days=1)
    sub = _subscribe(db, m.member_id, end_date=end)

    out = ChurnReasonService(db).submit(
        m.member_id,
        ChurnReasonIn(reason="expensive", subscribe_id=sub.subscribe_id, offer_shown=True),
    )

    assert out.reason == "expensive"
    assert out.offer_shown is True
    assert out.end_date_snapshot.replace(tzinfo=timezone.utc) == end, "sqlite 는 tz 를 안 돌려준다 — 값 자체만 비교"
    assert db.query(ChurnReason).count() == 1


def test_submit_upserts_last_value_on_resubmit(db):
    """★ 재설치 후 다른 답 → 마지막 값으로 덮어쓴다(행은 여전히 1개)."""
    m = _member(db)
    sub = _subscribe(db, m.member_id)
    svc = ChurnReasonService(db)

    svc.submit(m.member_id, ChurnReasonIn(
        reason="expensive", subscribe_id=sub.subscribe_id, offer_shown=False,
    ))
    out2 = svc.submit(m.member_id, ChurnReasonIn(
        reason="unused", subscribe_id=sub.subscribe_id, offer_shown=True,
    ))

    assert db.query(ChurnReason).count() == 1
    assert out2.reason == "unused"
    assert out2.offer_shown is True


def test_submit_rejects_someone_elses_subscribe(db):
    """⛔ 남의 subscribe_id 는 404 — 존재를 알리지 않는다."""
    owner = _member(db, "auth-owner")
    attacker = _member(db, "auth-attacker")
    sub = _subscribe(db, owner.member_id)

    with pytest.raises(HTTPException) as exc:
        ChurnReasonService(db).submit(
            attacker.member_id,
            ChurnReasonIn(reason="other", subscribe_id=sub.subscribe_id, offer_shown=False),
        )
    assert exc.value.status_code == 404
    assert db.query(ChurnReason).count() == 0


def test_submit_rejects_unknown_subscribe_id(db):
    m = _member(db)
    with pytest.raises(HTTPException) as exc:
        ChurnReasonService(db).submit(
            m.member_id,
            ChurnReasonIn(reason="missing", subscribe_id=999999, offer_shown=False),
        )
    assert exc.value.status_code == 404


def test_submit_rejects_subscribe_of_deleted_member(db):
    """⛔ subscribe.member_id 가 SET NULL(탈퇴)로 비어 있으면 아무도 소유자가 아니다."""
    owner = _member(db)
    sub = _subscribe(db, owner.member_id)
    sub.member_id = None  # 탈퇴 시뮬레이션(S2 — 실제로는 FK ondelete=SET NULL 이 이렇게 만든다)
    db.commit()

    m2 = _member(db, "auth-other")
    with pytest.raises(HTTPException) as exc:
        ChurnReasonService(db).submit(
            m2.member_id,
            ChurnReasonIn(reason="other", subscribe_id=sub.subscribe_id, offer_shown=False),
        )
    assert exc.value.status_code == 404


def test_reason_check_constraint_rejects_invalid_value(db):
    """DB CHECK 는 스키마 검증(Literal)을 우회해도(직접 ORM 조작) 마지막 방어선."""
    m = _member(db)
    db.add(ChurnReason(member_id=m.member_id, subscribe_id=None, reason="bogus", offer_shown=False))
    with pytest.raises(IntegrityError):
        db.commit()


# --------------------------------------------------------------------------- #
# 2) 라우터 계층 — 인증·소유·응답 계약
# --------------------------------------------------------------------------- #
def _build_app(session_factory):
    from main import create_app
    from core.config import settings as app_settings

    app = create_app()
    app.state.session_factory = session_factory
    app.state.settings = app_settings
    app.state.genai_client = object()
    return app


def test_churn_reason_endpoint_success(session_factory):
    from fastapi.testclient import TestClient

    db = session_factory()
    try:
        m = _member(db, "auth-me")
        sub = _subscribe(db, m.member_id)
        subscribe_id = sub.subscribe_id
    finally:
        db.close()

    app = _build_app(session_factory)
    client = TestClient(app)
    r = client.post(
        "/api/v1/members/me/churn-reasons",
        json={"reason": "missing", "subscribe_id": subscribe_id, "offer_shown": False},
        headers={"Authorization": "Bearer auth-me"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["reason"] == "missing"
    assert body["subscribe_id"] == subscribe_id
    assert body["offer_shown"] is False


def test_churn_reason_endpoint_rejects_other_members_subscribe(session_factory):
    from fastapi.testclient import TestClient

    db = session_factory()
    try:
        owner = _member(db, "auth-owner2")
        _member(db, "auth-intruder")
        sub = _subscribe(db, owner.member_id)
        subscribe_id = sub.subscribe_id
    finally:
        db.close()

    app = _build_app(session_factory)
    client = TestClient(app)
    r = client.post(
        "/api/v1/members/me/churn-reasons",
        json={"reason": "other", "subscribe_id": subscribe_id, "offer_shown": False},
        headers={"Authorization": "Bearer auth-intruder"},
    )
    assert r.status_code == 404


def test_churn_reason_endpoint_rejects_invalid_reason_with_422(session_factory):
    from fastapi.testclient import TestClient

    db = session_factory()
    try:
        m = _member(db, "auth-bad-reason")
        sub = _subscribe(db, m.member_id)
        subscribe_id = sub.subscribe_id
    finally:
        db.close()

    app = _build_app(session_factory)
    client = TestClient(app)
    r = client.post(
        "/api/v1/members/me/churn-reasons",
        json={"reason": "otherApp", "subscribe_id": subscribe_id, "offer_shown": False},
        headers={"Authorization": "Bearer auth-bad-reason"},
    )
    assert r.status_code == 422, "카멜(otherApp)을 받아주면 안 된다 — 서버 와이어는 snake(other_app)"
