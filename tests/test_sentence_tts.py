"""문장 단건 온디맨드 TTS 엔드포인트 결정적 테스트 (외부 의존 0).

검증 대상:
    POST /api/v1/sentences/{sentence_id}/tts
    - 신규 합성 → 200 + **object key** 저장(URL 이 아니다).
    - idempotent: voice_url 이 이미 있으면 재합성 없이 **재서명**해 반환.
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
                      # 과거에 **전체 URL 을 저장한 행**(2026-08-30 실측 형태).
                      # 백필 전에도 읽는 즉시 재서명돼야 한다.
                      voice_url=(
                          "https://storage.googleapis.com/beavertalk-app-audio"
                          "/voice-samples/tts/992/820.mp3?X-Goog-Expires=604800"
                      ),
                      evaluation=Evaluation())
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

    monkeypatch.setattr(ssvc.tts, "synthesize", _fake_tts)
    monkeypatch.setattr(ssvc.storage, "upload", lambda *a, **k: "tts/1/1.mp3")
    monkeypatch.setattr(
        ssvc.storage, "playback_url", lambda *a, **k: "https://stub/tts.mp3"
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


def test_tts_new_synthesis_returns_url_and_persists_key(
    session_factory, seeded, mock_tts_ok
):
    """응답은 서명 URL, **DB 에 남는 것은 object key**.

    ⛔ 여기서 URL 을 저장하면 서명이 만료된 뒤 영구히 재생 불가가 된다
    (2026-08-30 운영 결함). 이 단언이 그 회귀를 막는 자리다.
    """
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
        assert db.get(Sentence, sid).voice_url == "tts/1/1.mp3"
    finally:
        db.close()


def test_tts_idempotent_resigns_instead_of_echoing_stored_url(
    session_factory, seeded, monkeypatch
):
    """재합성은 안 하되 **저장값을 그대로 돌려주지도 않는다.**

    s2 는 과거에 전체 URL 을 저장한 행이다. 응답은 그 문자열이 아니라
    key 를 되짚어 **지금 서명한** URL 이어야 한다.
    """
    async def _boom(*_a, **_k):
        raise AssertionError("재합성하면 안 됨")

    monkeypatch.setattr(ssvc.tts, "synthesize", _boom)
    # playback_url 은 진짜를 쓰고 서명 단계만 스텁 — 정규화 경로를 실제로 태운다.
    monkeypatch.setattr(
        ssvc.storage, "signed_url", lambda bucket, key, ttl: f"https://resigned/{key}"
    )

    app = _build_app(session_factory)
    client = TestClient(app)
    sid = seeded["s2"]

    r = client.post(f"/api/v1/sentences/{sid}/tts", headers=_hdr())
    assert r.status_code == 200
    # 저장된 만료 URL 이 그대로 새어 나오면 실패한다.
    assert r.json()["voice_url"] == "https://resigned/tts/992/820.mp3"


def test_tts_empty_korean_returns_422(session_factory, seeded, mock_tts_ok):
    app = _build_app(session_factory)
    client = TestClient(app)
    r = client.post(f"/api/v1/sentences/{seeded['s3']}/tts", headers=_hdr())
    assert r.status_code == 422


def test_tts_synthesis_failure_returns_503(session_factory, seeded, monkeypatch):
    async def _fail(*_a, **_k):
        return None

    monkeypatch.setattr(ssvc.tts, "synthesize", _fail)
    app = _build_app(session_factory)
    client = TestClient(app)
    r = client.post(f"/api/v1/sentences/{seeded['s1']}/tts", headers=_hdr())
    assert r.status_code == 503


def test_tts_upload_failure_returns_503(session_factory, seeded, monkeypatch):
    async def _ok(*_a, **_k):
        return (b"\x00\x01", "audio/mpeg")

    monkeypatch.setattr(ssvc.tts, "synthesize", _ok)
    monkeypatch.setattr(ssvc.storage, "upload", lambda *a, **k: None)
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
