"""복습 채점 apply_score 플래그(점수 미갱신 모드) 결정적 테스트 (외부 의존 0).

검증 대상:
    POST /api/v1/sentences/{sentence_id}/reviews         (JSON, ReviewCreate)
    - apply_score=false → Review 저장(counted=false) + 채점 반환, sentence.evaluation 불변.
    - apply_score=true(기본) → 기존과 동일: counted=true + evaluation 갱신.

SpeechSuper 채점은 결정적 스텁으로 모킹(네트워크 0). 인증은 Supabase 토큰 검증 스텁.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Integer, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.evaluation import Evaluation
from domains.learning.models.review import Review
from domains.learning.models.sentence import Sentence

from core.config import settings as app_settings
from core.supabase_auth import AuthUser

import core.deps as deps
import domains.learning.service.review_service as rsvc


# 인증: Bearer 토큰 == auth uuid 로 취급("auth-*" 만 유효).
def _fake_verify(token):
    if token and token.startswith("auth-"):
        return AuthUser(uid=token, email=f"{token}@test.io")
    return None


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(deps, "verify_token", _fake_verify)


# 채점: 결정적 스텁(글자별 점수 + 평가 점수). 실제 SpeechSuper 미호출.
_STUB_FEEDBACK = {
    "evaluation": {
        "total_score": 88,
        "pronunciation": 90,
        "fluency": 85,
        "rhythm": 80,
    },
    "char_scores": [{"char": "안", "score": 88, "grade": "상"}],
}


@pytest.fixture(autouse=True)
def _mock_scoring(monkeypatch):
    monkeypatch.setattr(rsvc, "assess_pronunciation", lambda *_a, **_k: _STUB_FEEDBACK)
    # 스토리지 미사용(voice_url 키 그대로 저장, signed_url 폴백).
    monkeypatch.setattr(rsvc.storage, "signed_url", lambda *_a, **_k: None)


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
    """Voice/Character/Member + call + sentence(evaluation 초기값 有) 시드."""
    db = session_factory()
    try:
        voice = Voice(name="Fenrir", gender="male")
        db.add(voice)
        db.flush()
        ch = Character(name="비비", role="선생님", personality="다정",
                       voice_id=voice.voice_id, price=0)
        db.add(ch)
        db.flush()
        member = Member(language="en", korean_level=1, onboarding_completed=True,
                        auth_user_id="auth-member")
        db.add(member)
        db.flush()
        call = Call(member_id=member.member_id, character_id=ch.character_id,
                    status="done")
        db.add(call)
        db.flush()
        # 초기 공식점수(total_score=10) — 갱신 여부를 관찰하기 위한 기준값.
        s = Sentence(
            call_id=call.call_id, korean_sentence="안녕하세요",
            native_sentence="hi", locale="en",
            evaluation=Evaluation(total_score=10, pronunciation=10,
                                  fluency=10, rhythm=10),
        )
        db.add(s)
        db.commit()
        return {"member_id": member.member_id, "sid": s.sentence_id}
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


def test_apply_score_false_keeps_evaluation_and_marks_uncounted(
    session_factory, seeded
):
    """apply_score=false → Review 저장(counted=false)·채점 반환, 공식점수 불변."""
    app = _build_app(session_factory)
    client = TestClient(app)
    sid = seeded["sid"]

    r = client.post(
        f"/api/v1/sentences/{sid}/reviews",
        json={"voice_url": "reviews/x.mp3", "apply_score": False},
        headers=_hdr(),
    )
    assert r.status_code == 201, r.text
    # 채점 자체는 반환된다(연습 모드도 피드백은 준다).
    assert r.json()["evaluation"]["total_score"] == 88

    db = session_factory()
    try:
        review = db.scalars(select(Review).where(Review.sentence_id == sid)).one()
        assert review.counted is False              # 미산입
        assert review.voice_url == "reviews/x.mp3"  # 이력·음성은 저장
        assert review.feedback == _STUB_FEEDBACK    # phonemes/char 포함 저장
        ev = db.get(Sentence, sid).evaluation
        assert ev.total_score == 10                 # 공식점수 불변
        assert ev.pronunciation == 10
    finally:
        db.close()


def test_apply_score_true_updates_evaluation_and_marks_counted(
    session_factory, seeded
):
    """apply_score=true(기본) → counted=true + evaluation 마지막 시도 점수로 갱신."""
    app = _build_app(session_factory)
    client = TestClient(app)
    sid = seeded["sid"]

    r = client.post(
        f"/api/v1/sentences/{sid}/reviews",
        json={"voice_url": "reviews/y.mp3", "apply_score": True},
        headers=_hdr(),
    )
    assert r.status_code == 201, r.text

    db = session_factory()
    try:
        review = db.scalars(select(Review).where(Review.sentence_id == sid)).one()
        assert review.counted is True
        ev = db.get(Sentence, sid).evaluation
        assert ev.total_score == 88                 # 갱신됨
        assert ev.pronunciation == 90
        assert ev.rhythm == 80
    finally:
        db.close()


def test_apply_score_default_is_true_backward_compat(session_factory, seeded):
    """필드 생략 시 기존 동작과 100% 동일 — counted=true + evaluation 갱신."""
    app = _build_app(session_factory)
    client = TestClient(app)
    sid = seeded["sid"]

    r = client.post(
        f"/api/v1/sentences/{sid}/reviews",
        json={"voice_url": "reviews/z.mp3"},
        headers=_hdr(),
    )
    assert r.status_code == 201, r.text

    db = session_factory()
    try:
        review = db.scalars(select(Review).where(Review.sentence_id == sid)).one()
        assert review.counted is True
        assert db.get(Sentence, sid).evaluation.total_score == 88
    finally:
        db.close()
