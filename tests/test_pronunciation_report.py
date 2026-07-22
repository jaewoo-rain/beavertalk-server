"""발음 리포트 엔드포인트 결정적 테스트 (외부 의존 0).

GET /api/v1/calls/{call_id}/pronunciation-report
    - 본인 통화 → 200 + LearningSummary 형태(통과·문장별·소리별·최근세션).
    - 평가 점수 있으면 실값, 없으면 결정적 목값 폴백.
    - 타인/없는 통화 → 404.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401 (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.evaluation import Evaluation
from domains.learning.models.sentence import Sentence

from core.config import settings as app_settings
from core.supabase_auth import AuthUser

import core.deps as deps


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


def _mk_call(db, member_id, ch_id, when, scores):
    """scores: list of total_score(or None) → 문장 N개 + 평가."""
    call = Call(member_id=member_id, character_id=ch_id, status="done", call_date=when)
    db.add(call)
    db.flush()
    for i, sc in enumerate(scores):
        ev = Evaluation(total_score=sc, pronunciation=sc, fluency=sc, rhythm=sc) if sc is not None else Evaluation()
        db.add(Sentence(call_id=call.call_id, korean_sentence=f"문장{i}", locale="en", evaluation=ev))
    db.flush()
    return call


@pytest.fixture()
def seeded(session_factory):
    db = session_factory()
    try:
        voice = Voice(name="Fenrir", gender="male")
        db.add(voice)
        db.flush()
        ch = Character(name="Baba", role="선생님", personality="다정", voice_id=voice.voice_id, price=0)
        db.add(ch)
        db.flush()
        member = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-member")
        other = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-other")
        db.add_all([member, other])
        db.flush()

        # 대상 통화: 4문장, 3개 통과(≥80), 1개 미달
        target = _mk_call(db, member.member_id, ch.character_id,
                          datetime(2026, 7, 20, tzinfo=timezone.utc), [98, 71, 92, 89])
        # 과거 세션 2건(최근세션 집계용)
        _mk_call(db, member.member_id, ch.character_id, datetime(2026, 7, 10, tzinfo=timezone.utc), [80, 84])
        _mk_call(db, member.member_id, ch.character_id, datetime(2026, 7, 15, tzinfo=timezone.utc), [90, 88])
        db.commit()
        return {"member": member.member_id, "call": target.call_id}
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


def test_report_returns_learning_summary_shape(session_factory, seeded):
    client = TestClient(_build_app(session_factory))
    r = client.get(f"/api/v1/calls/{seeded['call']}/pronunciation-report", headers=_hdr())
    assert r.status_code == 200, r.text
    b = r.json()

    # 통과·총합(실데이터): 4문장, 98/92/89 통과 = 3
    assert b["total"] == 4
    assert b["passed"] == 3
    # 문장별(실데이터)
    assert len(b["sentences"]) == 4
    assert b["sentences"][0]["pronunciation"] == 98
    # 소리별 정확도(목)
    assert len(b["phonemes"]) == 4
    assert b["phonemes"][0]["sound"] == "받침 ㄹ"
    # 최근 세션(실데이터): 통화 3건, oldest first, 첫 delta=None
    assert len(b["sessions"]) == 3
    assert b["sessions"][0]["delta"] is None
    assert b["sessions"][-1]["label"]  # 최신 라벨 존재
    # 문장/발음/유창/리듬 키 존재
    assert {"hardest_sound", "hardest_evidence", "l1_interference"} <= b.keys()


def test_report_other_member_404(session_factory, seeded):
    client = TestClient(_build_app(session_factory))
    r = client.get(f"/api/v1/calls/{seeded['call']}/pronunciation-report", headers=_hdr("auth-other"))
    assert r.status_code == 404


def test_report_unknown_call_404(session_factory, seeded):
    client = TestClient(_build_app(session_factory))
    r = client.get("/api/v1/calls/999999/pronunciation-report", headers=_hdr())
    assert r.status_code == 404
