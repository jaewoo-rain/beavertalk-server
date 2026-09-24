"""POST /api/v1/subscriptions — R3-a(2026-09-24, bt-back, 판매 개시 전 필수) admin 전용화.

배경: 이 API 는 IAP 전환 전 임시 개발 통로다(`SubscriptionService.start` 문서 참조,
금액 검증은 `gt=0` 뿐). 2단화로 유료 기능이 실제로 갈린 지금은, 아무 회원이나
본인 JWT 로 이 API 를 한 번 불러 `is_activate=True, plan="premium"` 행을 스스로
만들 수 있었다 — 실결제 없이 영상·15분·전 캐릭터를 지급받는 구멍. admin 전용으로
좁혀 개발 통로는 유지하고 구멍만 막는다.

`GET /subscriptions`·`GET /subscriptions/status`·`POST /{id}/cancel` 은 이 시험의
대상이 아니다 — 그건 본인 구독을 읽고/취소하는 것뿐이라 회원 접근이 여전히 맞다
(R3-a 는 "행을 스스로 만드는" 경로만 막는다).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.deps as deps
from core.config import settings as app_settings
from core.supabase_auth import AuthUser
from db.registry import Base
from domains.account.models.member import Member


def _fake_verify(token):
    return AuthUser(uid=token, email=f"{token}@test.io") if token and token.startswith("auth-") else None


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    monkeypatch.setattr(deps, "verify_token", _fake_verify)


@pytest.fixture()
def env():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            if len(t.primary_key.columns) == 1:
                pk.type = Integer()
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = sf()
    member = Member(language="en", onboarding_completed=True, auth_user_id="auth-plain", role="user")
    admin = Member(language="en", onboarding_completed=True, auth_user_id="auth-admin", role="admin")
    db.add(member); db.add(admin); db.commit()
    db.close()
    from main import create_app
    app = create_app()
    app.state.session_factory = sf
    app.state.settings = app_settings
    app.state.genai_client = object()
    return TestClient(app)


def _post(client, token):
    return client.post(
        "/api/v1/subscriptions",
        json={"price": "9.99", "plan": "premium"},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_non_admin_cannot_self_grant_premium(env):
    """⛔⛔ 핵심 재현·수정 확인 — 비admin 이 이 API 를 불러 스스로 premium 을 켤 수
    없어야 한다(그 구멍)."""
    r = _post(env, "auth-plain")
    assert r.status_code == 403


def test_admin_can_still_use_the_dev_channel(env):
    """개발 통로 유지 — 사장님(admin)은 테스트로 premium 을 켤 수 있어야 한다."""
    r = _post(env, "auth-admin")
    assert r.status_code == 201, r.text
    assert r.json()["is_activate"] is True
