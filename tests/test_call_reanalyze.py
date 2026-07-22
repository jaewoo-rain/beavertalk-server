"""POST /api/v1/calls/{call_id}/reanalyze — 실패 통화 수동 재분석 엔드포인트 (외부 의존 0).

검증 대상:
    - 'failed' 통화 → 200 + status='analyzing' 리셋 + 백그라운드 트리거 호출(call_type/locale 전달).
    - 'done'/'analyzing'/'ongoing' → 409(재분석 대상 아님).
    - 타인/없는 통화 → 404.
    - genai client 미준비 → 503.
    - level_test 통화 → 트리거에 call_type='level_test' 전달.

백그라운드 분석 트리거(trigger_reanalysis)는 레코더로 모킹(실제 Gemini 태스크 미생성).
인증은 Supabase 토큰 검증을 스텁한다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from core.config import settings as app_settings
from core.supabase_auth import AuthUser
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call

import core.deps as deps
import domains.learning.routers.call as call_router


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


def _seed(session_factory, *, status="failed", call_type="normal", owner="auth-member"):
    """Voice/Character/Member(+타인) + call(지정 status/call_type) 시드 → ids 반환."""
    db = session_factory()
    try:
        voice = Voice(name="Fenrir", gender="male")
        db.add(voice)
        db.flush()
        ch = Character(name="바바", role="선생님", personality="시크",
                       voice_id=voice.voice_id, price=0)
        db.add(ch)
        db.flush()
        member = Member(language="en", korean_level=1, onboarding_completed=True,
                        auth_user_id=owner)
        other = Member(language="en", korean_level=1, onboarding_completed=True,
                       auth_user_id="auth-other")
        db.add_all([member, other])
        db.flush()
        call = Call(member_id=member.member_id, character_id=ch.character_id,
                    status=status, call_type=call_type)
        db.add(call)
        db.commit()
        return {"call_id": call.call_id, "member_id": member.member_id,
                "other_id": other.member_id}
    finally:
        db.close()


def _build_app(session_factory, genai_client=object()):
    from main import create_app
    app = create_app()
    app.state.session_factory = session_factory
    app.state.settings = app_settings
    app.state.genai_client = genai_client
    return app


def _hdr(auth="auth-member"):
    return {"Authorization": f"Bearer {auth}"}


@pytest.fixture()
def recorder(monkeypatch):
    """trigger_reanalysis 를 레코더로 대체(실제 백그라운드 분석 태스크 미생성)."""
    calls = []

    def _rec(settings, client, session_factory, locale, *, call_id, call_type, member_id):
        calls.append({"locale": locale, "call_id": call_id,
                      "call_type": call_type, "member_id": member_id})

    monkeypatch.setattr(call_router, "trigger_reanalysis", _rec)
    return calls


def _status_in_db(session_factory, call_id):
    db = session_factory()
    try:
        return db.get(Call, call_id).status
    finally:
        db.close()


# --------------------------------------------------------------------------- #
def test_reanalyze_failed_call_resets_and_triggers(session_factory, recorder):
    ids = _seed(session_factory, status="failed", call_type="normal")
    app = _build_app(session_factory)
    client = TestClient(app)

    r = client.post(f"/api/v1/calls/{ids['call_id']}/reanalyze", headers=_hdr())

    assert r.status_code == 200
    assert r.json() == {"call_id": ids["call_id"], "status": "analyzing"}
    # status 가 실제로 리셋됐고(커밋), 백그라운드 트리거가 호출됐다.
    assert _status_in_db(session_factory, ids["call_id"]) == "analyzing"
    assert len(recorder) == 1
    assert recorder[0]["call_type"] == "normal"
    assert recorder[0]["locale"] == "en"
    assert recorder[0]["member_id"] == ids["member_id"]


def test_reanalyze_level_test_passes_call_type(session_factory, recorder):
    ids = _seed(session_factory, status="failed", call_type="level_test")
    app = _build_app(session_factory)
    client = TestClient(app)

    r = client.post(f"/api/v1/calls/{ids['call_id']}/reanalyze", headers=_hdr())

    assert r.status_code == 200
    assert recorder[0]["call_type"] == "level_test"


@pytest.mark.parametrize("status", ["done", "analyzing", "ongoing"])
def test_reanalyze_non_failed_returns_409(session_factory, recorder, status):
    ids = _seed(session_factory, status=status)
    app = _build_app(session_factory)
    client = TestClient(app)

    r = client.post(f"/api/v1/calls/{ids['call_id']}/reanalyze", headers=_hdr())

    assert r.status_code == 409
    # 상태 불변 + 트리거 미호출.
    assert _status_in_db(session_factory, ids["call_id"]) == status
    assert recorder == []


def test_reanalyze_other_members_call_404(session_factory, recorder):
    ids = _seed(session_factory, status="failed")
    app = _build_app(session_factory)
    client = TestClient(app)

    # 타인(auth-other)으로 남의 통화 재분석 시도 → 404(소유권 가드).
    r = client.post(f"/api/v1/calls/{ids['call_id']}/reanalyze", headers=_hdr("auth-other"))

    assert r.status_code == 404
    assert _status_in_db(session_factory, ids["call_id"]) == "failed"
    assert recorder == []


def test_reanalyze_missing_call_404(session_factory, recorder):
    _seed(session_factory)
    app = _build_app(session_factory)
    client = TestClient(app)

    r = client.post("/api/v1/calls/999999/reanalyze", headers=_hdr())

    assert r.status_code == 404
    assert recorder == []


def test_reanalyze_no_genai_client_503(session_factory, recorder):
    ids = _seed(session_factory, status="failed")
    app = _build_app(session_factory, genai_client=None)  # 분석 스택 미준비
    client = TestClient(app)

    r = client.post(f"/api/v1/calls/{ids['call_id']}/reanalyze", headers=_hdr())

    assert r.status_code == 503
    # 미준비면 status 도 안 건드린다(트리거 미호출).
    assert _status_in_db(session_factory, ids["call_id"]) == "failed"
    assert recorder == []
