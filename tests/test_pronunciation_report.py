"""발음 리포트 어댑터 테스트 — main pronunciation_service 실데이터 → LearningSummary.

GET /api/v1/calls/{call_id}/pronunciation-report
    - pronunciation_service.get_pronunciation_report/history 를 가짜로 주입하고,
      어댑터가 통과수·평균·가장 어려웠던 소리·소리별 정확도(2+2)·세션 delta 로 잘 가공하는지.
    - main 리포트 None(없는 통화) → 404.
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

from core.config import settings as app_settings
from core.supabase_auth import AuthUser
from domains.learning.schemas.pronunciation import (
    PronHistoryItem,
    PronSentenceScore,
    PronunciationReport,
    SoundAggregate,
)

import core.deps as deps
import domains.learning.service.pronunciation_service as pron_module


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
        db.add(member)
        db.flush()
        call = Call(member_id=member.member_id, character_id=ch.character_id, status="done",
                    call_date=datetime(2026, 7, 22, tzinfo=timezone.utc))
        db.add(call)
        db.commit()
        return {"member": member.member_id, "call": call.call_id}
    finally:
        db.close()


def _fake_report(call_id: int):
    return PronunciationReport(
        call_id=call_id,
        country="United States",
        sentences=[
            PronSentenceScore(sentence_id=1, korean_sentence="문장A", total_score=98, pronunciation=98, fluency=95, rhythm=97),
            PronSentenceScore(sentence_id=2, korean_sentence="문장B", total_score=71, pronunciation=71, fluency=88, rhythm=84),
            PronSentenceScore(sentence_id=3, korean_sentence="문장C", total_score=92, pronunciation=94, fluency=90, rhythm=92),
            PronSentenceScore(sentence_id=4, korean_sentence="문장D", total_score=89, pronunciation=89, fluency=86, rhythm=91),
        ],
        sounds=[
            SoundAggregate(alpha="ㄹ", attempts=7, passes=3, pronunciation_avg=43.0),   # 정확도 43(최저)
            SoundAggregate(alpha="ㄱ", attempts=10, passes=6, pronunciation_avg=60.0),  # 60
            SoundAggregate(alpha="ㅔ", attempts=8, passes=5, pronunciation_avg=63.0),   # 63
            SoundAggregate(alpha="ㅗ", attempts=12, passes=9, pronunciation_avg=75.0),  # 75(시도 최다)
            SoundAggregate(alpha="ㅇ", attempts=9, passes=8, pronunciation_avg=89.0),   # 89
        ],
        comment="종성 ㄹ이 모국어에 없어 어려운 거예요. 당신 잘못이 아니에요.",
    )


def _fake_history(db, member_id):
    # 최신순(get_pronunciation_history 계약)
    return [
        PronHistoryItem(call_id=3, call_date=datetime(2026, 7, 22, tzinfo=timezone.utc), sentence_count=10, score=97.0),
        PronHistoryItem(call_id=2, call_date=datetime(2026, 7, 18, tzinfo=timezone.utc), sentence_count=8, score=84.0),
        PronHistoryItem(call_id=1, call_date=datetime(2026, 7, 15, tzinfo=timezone.utc), sentence_count=9, score=80.0),
    ]


@pytest.fixture()
def _patch_pron(monkeypatch):
    async def _report(**kw):
        return _fake_report(kw["call_id"])
    monkeypatch.setattr(pron_module, "get_pronunciation_report", _report)
    monkeypatch.setattr(pron_module, "get_pronunciation_history", _fake_history)


def _build_app(session_factory):
    from main import create_app
    app = create_app()
    app.state.session_factory = session_factory
    app.state.settings = app_settings
    app.state.genai_client = object()
    return app


def _hdr(auth="auth-member"):
    return {"Authorization": f"Bearer {auth}"}


def test_report_adapts_main_data(session_factory, seeded, _patch_pron):
    client = TestClient(_build_app(session_factory))
    r = client.get(f"/api/v1/calls/{seeded['call']}/pronunciation-report", headers=_hdr())
    assert r.status_code == 200, r.text
    b = r.json()

    # 통과·총합(실 sentences)
    assert b["total"] == 4
    assert b["passed"] == 3  # 98/92/89 통과, 71 탈락
    # 소리별 정확도: 정확도 낮은 2개(ㄹ·ㄱ) + 시도 많은 2개(ㅗ·ㅇ)
    assert [p["sound"] for p in b["phonemes"]] == ["ㄹ", "ㄱ", "ㅗ", "ㅇ"]
    assert b["phonemes"][0]["correct"] == 3
    # 가장 어려웠던 소리 = 정확도 최저 ㄹ, evidence 동적
    assert b["hardest_sound"] == "ㄹ"
    assert "7번 중 4번" in b["hardest_evidence"]
    # L1 피드백 = main 의 comment(진짜)
    assert b["l1_interference"].startswith("종성 ㄹ")
    # 최근 세션: oldest first, 첫 delta None, 이후 +4/+13
    assert [s["score"] for s in b["sessions"]] == [80, 84, 97]
    assert b["sessions"][0]["delta"] is None
    assert b["sessions"][1]["delta"] == 4


def test_report_unknown_call_404(session_factory, seeded, monkeypatch):
    async def _none(**kw):
        return None
    monkeypatch.setattr(pron_module, "get_pronunciation_report", _none)
    client = TestClient(_build_app(session_factory))
    r = client.get(f"/api/v1/calls/{seeded['call']}/pronunciation-report", headers=_hdr())
    assert r.status_code == 404
