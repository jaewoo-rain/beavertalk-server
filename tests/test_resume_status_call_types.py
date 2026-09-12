"""GET /api/v1/calls/{id}/resume-status — can_resume 는 normal·expression·freetalk 에 열리고 level_test 는 닫힌다(2026-09-12, 실기기 1447).

옛 조건이 normal 만 허용해 표현학습·프리토킹은 5분에 «Keep talking» 시트 없이 결과 화면으로 떨어졌다. 조각 상한·요약 준비 판정은 스텁.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.deps as deps
import domains.learning.routers.call as call_router
from core.config import settings as app_settings
from core.supabase_auth import AuthUser
from db.registry import Base
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call


def _fake_verify(token):
    return AuthUser(uid=token, email=f"{token}@test.io") if token and token.startswith("auth-") else None


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    monkeypatch.setattr(deps, "verify_token", _fake_verify)
    monkeypatch.setattr(call_router.call_service, "call_fragments_for_member", lambda db, member_id: 3)   # Pro·Max 상한
    monkeypatch.setattr(call_router.svc, "resume_context_is_fresh", lambda db, call_id: False)


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
    v = Voice(name="Fenrir", gender="male"); db.add(v); db.flush()
    ch = Character(name="비비", role="선생님", personality="다정", voice_id=v.voice_id, price=0); db.add(ch); db.flush()
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-m"); db.add(m); db.commit()
    calls = {}
    for ct, frags in (("normal", 1), ("expression", 1), ("freetalk", 2), ("level_test", 1), ("expression", 3)):
        c = Call(member_id=m.member_id, character_id=ch.character_id, call_type=ct, status="done", fragment_count=frags)
        db.add(c); db.flush()
        calls[(ct, frags)] = c.call_id
    db.commit(); db.close()
    from main import create_app
    app = create_app()
    app.state.session_factory = sf
    app.state.settings = app_settings
    app.state.genai_client = object()
    return TestClient(app), calls


def _status(client, call_id):
    r = client.get(f"/api/v1/calls/{call_id}/resume-status", headers={"Authorization": "Bearer auth-m"})
    assert r.status_code == 200, r.text
    return r.json()


def test_expression_and_freetalk_can_resume_like_normal_when_fragments_remain(env):
    client, calls = env
    for key in (("normal", 1), ("expression", 1), ("freetalk", 2)):
        body = _status(client, calls[key])
        assert body["can_resume"] is True, key
        assert body["fragment_count"] == key[1] and body["max_fragments"] == 3


def test_level_test_never_resumes_and_exhausted_fragments_close_the_door(env):
    client, calls = env
    assert _status(client, calls[("level_test", 1)])["can_resume"] is False
    assert _status(client, calls[("expression", 3)])["can_resume"] is False, "조각 상한 소진"
