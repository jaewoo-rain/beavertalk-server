"""문장 단건 온디맨드 TTS 엔드포인트 결정적 테스트 (외부 의존 0).

검증 대상:
    POST /api/v1/sentences/{sentence_id}/tts
    - 신규 합성 → 200 + voice_url 저장.
    - idempotent: voice_url 이 이미 있으면 재합성 없이 그대로 반환.
    - 빈 korean_sentence → 422.
    - genai client None → 503.
    - 합성 실패(None) → 503.
    - 타인/없는 문장 → 404.

TTS/Storage/genai 는 모두 모킹(네트워크 0). 인증은 Supabase 토큰 검증을 스텁한다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.evaluation import Evaluation
from domains.learning.models.sentence import Sentence

from core.config import settings as app_settings
from core.supabase_auth import AuthUser

import core.deps as deps
import domains.learning.service.sentence_service as ssvc


# 인증: Bearer 토큰 == auth uuid 로 취급("auth-*" 만 유효).
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
    """Voice/Character/Member + call + sentence(2건) 시드.

    s1: korean 있음/voice_url 없음(신규 합성 대상)
    s2: voice_url 이미 있음(idempotent)
    s3: korean 비어있음(422)
    또 타인 회원 1명 시드.
    """
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
        other = Member(language="en", korean_level=1, onboarding_completed=True,
                       auth_user_id="auth-other")
        db.add_all([member, other])
        db.flush()
        call = Call(member_id=member.member_id, character_id=ch.character_id,
                    status="done")
        db.add(call)
        db.flush()
        s1 = Sentence(call_id=call.call_id, korean_sentence="안녕하세요",
                      native_sentence="hi", locale="en", evaluation=Evaluation())
        s2 = Sentence(call_id=call.call_id, korean_sentence="고맙습니다",
                      native_sentence="thanks", locale="en",
                      voice_url="https://existing/url.mp3", evaluation=Evaluation())
        s3 = Sentence(call_id=call.call_id, korean_sentence="   ",
                      native_sentence="", locale="en", evaluation=Evaluation())
        db.add_all([s1, s2, s3])
        db.commit()
        return {
            "member_id": member.member_id,
            "s1": s1.sentence_id,
            "s2": s2.sentence_id,
            "s3": s3.sentence_id,
        }
    finally:
        db.close()


@pytest.fixture()
def mock_tts_ok(monkeypatch):
    """합성 성공 + storage 스텁(public URL 반환)."""
    async def _fake_tts(*_a, **_k):
        return (b"\x00\x01" * 16, "audio/mpeg")

    monkeypatch.setattr(ssvc.tts, "synthesize_korean", _fake_tts)
    monkeypatch.setattr(ssvc.storage, "upload", lambda *a, **k: "tts/1/1.mp3")
    monkeypatch.setattr(
        ssvc.storage, "public_url", lambda *a, **k: "https://stub/tts.mp3"
    )


def _build_app(session_factory, genai_client=object()):
    from main import create_app
    app = create_app()
    app.state.session_factory = session_factory
    app.state.settings = app_settings
    app.state.genai_client = genai_client
    return app


def _hdr(auth="auth-member"):
    return {"Authorization": f"Bearer {auth}"}


def test_tts_new_synthesis_returns_url_and_persists(
    session_factory, seeded, mock_tts_ok
):
    app = _build_app(session_factory)
    client = TestClient(app)
    sid = seeded["s1"]

    r = client.post(f"/api/v1/sentences/{sid}/tts", headers=_hdr())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sentence_id"] == sid
    assert body["voice_url"] == "https://stub/tts.mp3"

    db = session_factory()
    try:
        assert db.get(Sentence, sid).voice_url == "https://stub/tts.mp3"
    finally:
        db.close()


def test_tts_idempotent_returns_existing_without_resynth(
    session_factory, seeded, monkeypatch
):
    # 합성이 호출되면 실패하도록 → 호출 안 됨을 보장.
    async def _boom(*_a, **_k):
        raise AssertionError("재합성하면 안 됨")

    monkeypatch.setattr(ssvc.tts, "synthesize_korean", _boom)

    app = _build_app(session_factory)
    client = TestClient(app)
    sid = seeded["s2"]

    r = client.post(f"/api/v1/sentences/{sid}/tts", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["voice_url"] == "https://existing/url.mp3"


def test_tts_empty_korean_returns_422(session_factory, seeded, mock_tts_ok):
    app = _build_app(session_factory)
    client = TestClient(app)
    r = client.post(f"/api/v1/sentences/{seeded['s3']}/tts", headers=_hdr())
    assert r.status_code == 422


def test_tts_no_genai_client_returns_503(session_factory, seeded, mock_tts_ok):
    app = _build_app(session_factory, genai_client=None)
    client = TestClient(app)
    r = client.post(f"/api/v1/sentences/{seeded['s1']}/tts", headers=_hdr())
    assert r.status_code == 503


def test_tts_synthesis_failure_returns_503(session_factory, seeded, monkeypatch):
    async def _fail(*_a, **_k):
        return None

    monkeypatch.setattr(ssvc.tts, "synthesize_korean", _fail)
    app = _build_app(session_factory)
    client = TestClient(app)
    r = client.post(f"/api/v1/sentences/{seeded['s1']}/tts", headers=_hdr())
    assert r.status_code == 503


def test_tts_upload_failure_returns_503(session_factory, seeded, monkeypatch):
    async def _ok(*_a, **_k):
        return (b"\x00\x01", "audio/mpeg")

    monkeypatch.setattr(ssvc.tts, "synthesize_korean", _ok)
    monkeypatch.setattr(ssvc.storage, "upload", lambda *a, **k: None)
    monkeypatch.setattr(ssvc.storage, "public_url", lambda *a, **k: None)
    app = _build_app(session_factory)
    client = TestClient(app)
    r = client.post(f"/api/v1/sentences/{seeded['s1']}/tts", headers=_hdr())
    assert r.status_code == 503


def test_tts_other_member_returns_404(session_factory, seeded, mock_tts_ok):
    app = _build_app(session_factory)
    client = TestClient(app)
    r = client.post(
        f"/api/v1/sentences/{seeded['s1']}/tts", headers=_hdr("auth-other")
    )
    assert r.status_code == 404


def test_tts_unknown_sentence_returns_404(session_factory, seeded, mock_tts_ok):
    app = _build_app(session_factory)
    client = TestClient(app)
    r = client.post("/api/v1/sentences/999999/tts", headers=_hdr())
    assert r.status_code == 404
