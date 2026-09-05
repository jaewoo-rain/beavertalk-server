# -*- coding: utf-8 -*-
"""온디맨드 TTS(`POST /api/v1/tts/speech`) 회귀.

## 이 API 가 지키는 계약 두 개

1. **200 일 때만 오디오**다. 나머지는 전부 JSON 에러 — 상태코드로 갈린다.
2. **저장을 안 한다.** 그래서 `ETag`/`304` 가 서버가 가진 **유일한 절약**이고,
   그게 깨지면 조용히 매번 재합성한다(로그에도 안 남는다).
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

import core.deps as deps
import core.tts as tts_mod


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


def _seed(session_factory, *, rep_voice="Leda", other_voice="Fenrir", target="한국어"):
    """대표 캐릭터 + 다른 캐릭터 + 회원 시드."""
    db = session_factory()
    try:
        v1 = Voice(name=rep_voice, gender="female")
        v2 = Voice(name=other_voice, gender="male")
        db.add_all([v1, v2])
        db.flush()
        rep = Character(name="바바", role="선생님", personality="시크",
                        voice_id=v1.voice_id, price=0)
        oth = Character(name="비비", role="친구", personality="발랄",
                        voice_id=v2.voice_id, price=0)
        db.add_all([rep, oth])
        db.flush()
        m = Member(language="en", korean_level=1, onboarding_completed=True,
                   auth_user_id="auth-member", character_id=rep.character_id,
                   target_language=target)
        db.add(m)
        db.commit()
        return {"member_id": m.member_id, "rep_id": rep.character_id,
                "other_id": oth.character_id}
    finally:
        db.close()


def _build_app(session_factory):
    from main import create_app
    app = create_app()
    app.state.session_factory = session_factory
    app.state.settings = app_settings
    app.state.genai_client = object()
    return app


def _hdr(**extra):
    h = {"Authorization": "Bearer auth-member"}
    h.update(extra)
    return h


@pytest.fixture()
def spy(monkeypatch):
    """`core.tts.synthesize` 를 가로채 (호출인자, 호출횟수) 를 본다."""
    calls = []

    async def fake(text, language="ko", voice=None):
        calls.append({"text": text, "language": language, "voice": voice})
        return (b"ID3-FAKE-MP3-BYTES", "audio/mpeg")

    monkeypatch.setattr(tts_mod, "synthesize", fake)
    return calls


# --------------------------------------------------------------------------- #
# 1. 계약 ① — 200 이면 바이너리다
# --------------------------------------------------------------------------- #

def test_ok_returns_raw_mp3_bytes_not_json(session_factory, spy):
    """⛔ 200 의 바디는 **MP3 바이트**다. JSON 이 아니다.

    `response_model` 을 붙이면 여기서 깨진다 — 그래서 라우터에 안 붙였고, 이 시험이
    그 결정을 지킨다.
    """
    _seed(session_factory)
    c = TestClient(_build_app(session_factory))

    r = c.post("/api/v1/tts/speech", json={"text": "안녕하세요"}, headers=_hdr())

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/mpeg")
    assert r.content == b"ID3-FAKE-MP3-BYTES"
    assert r.headers.get("ETag"), "ETag 가 없으면 304 절약이 아예 불가능하다"


# --------------------------------------------------------------------------- #
# 2. 목소리·언어를 서버가 정한다
# --------------------------------------------------------------------------- #

def test_omitting_character_id_uses_the_representative_character(session_factory, spy):
    """⭐ `character_id` 를 안 주면 **대표 캐릭터**(member.character_id) 목소리다.

    통화의 폴백 사슬과 같은 규칙이다 — 두 곳이 어긋나면 «통화에선 A 목소리인데 TTS 는
    B 목소리» 가 된다.
    """
    _seed(session_factory)
    c = TestClient(_build_app(session_factory))

    c.post("/api/v1/tts/speech", json={"text": "안녕"}, headers=_hdr())

    assert spy[0]["voice"] == "Leda"


def test_explicit_character_id_wins(session_factory, spy):
    ids = _seed(session_factory)
    c = TestClient(_build_app(session_factory))

    c.post("/api/v1/tts/speech",
           json={"text": "안녕", "character_id": ids["other_id"]}, headers=_hdr())

    assert spy[0]["voice"] == "Fenrir"


def test_language_comes_from_the_member_not_the_client(session_factory, spy):
    """⛔ 클라는 언어 코드를 안 보낸다 — `member.target_language` 가 단일 소스다."""
    _seed(session_factory, target="일본어")
    c = TestClient(_build_app(session_factory))

    c.post("/api/v1/tts/speech", json={"text": "안녕"}, headers=_hdr())

    assert spy[0]["language"] == "일본어"


# --------------------------------------------------------------------------- #
# 3. 에러 — 조용한 폴백이 없어야 한다
# --------------------------------------------------------------------------- #

def test_unknown_character_id_is_404_not_a_silent_fallback(session_factory, spy):
    """⛔ 없는 캐릭터를 주면 **404**. 조용히 대표 캐릭터로 떨어뜨리면 프론트는
    «내가 고른 목소리로 나왔다» 고 믿는다 — 틀린 성공이 제일 나쁘다.
    """
    _seed(session_factory)
    c = TestClient(_build_app(session_factory))

    r = c.post("/api/v1/tts/speech",
               json={"text": "안녕", "character_id": 99999}, headers=_hdr())

    assert r.status_code == 404
    assert spy == [], "404 인데 합성이 돌았다 — 원가가 샌다"


@pytest.mark.parametrize("text", ["", "   ", "가" * 201])
def test_bad_text_is_422(session_factory, spy, text):
    """길이 방어. ⚠ 원가가 글자 수에 비례하므로 이 상한이 1회 원가의 천장이다."""
    _seed(session_factory)
    c = TestClient(_build_app(session_factory))

    r = c.post("/api/v1/tts/speech", json={"text": text}, headers=_hdr())

    assert r.status_code == 422
    assert spy == []


def test_tts_unavailable_is_503_so_the_app_can_fall_back(session_factory, monkeypatch):
    """⛔ 키 부재·합성 실패는 **503**(R5 graceful degradation).

    서버가 죽지 않고 기능만 꺼진다. ⚠ 재시도로 풀리는 오류가 아니라, 앱이 «음성 없이
    텍스트만» 보여줄 수 있어야 한다.
    """
    _seed(session_factory)

    async def fake_none(text, language="ko", voice=None):
        return None

    monkeypatch.setattr(tts_mod, "synthesize", fake_none)
    c = TestClient(_build_app(session_factory))

    r = c.post("/api/v1/tts/speech", json={"text": "안녕"}, headers=_hdr())

    assert r.status_code == 503
    assert r.headers["content-type"].startswith("application/json"), \
        "에러는 JSON 이어야 한다 — 오디오로 주면 재생기가 조용히 실패한다"


def test_anonymous_is_401(session_factory, spy):
    _seed(session_factory)
    c = TestClient(_build_app(session_factory))

    r = c.post("/api/v1/tts/speech", json={"text": "안녕"})

    assert r.status_code == 401
    assert spy == []


# --------------------------------------------------------------------------- #
# 4. ⭐ 계약 ② — 304 가 유일한 서버측 절약이다
# --------------------------------------------------------------------------- #

def test_same_request_yields_the_same_etag(session_factory, spy):
    _seed(session_factory)
    c = TestClient(_build_app(session_factory))

    a = c.post("/api/v1/tts/speech", json={"text": "안녕"}, headers=_hdr())
    b = c.post("/api/v1/tts/speech", json={"text": "안녕"}, headers=_hdr())

    assert a.headers["ETag"] == b.headers["ETag"]


def test_if_none_match_returns_304_and_does_not_synthesize(session_factory, spy):
    """⭐⭐ **이 파일에서 가장 중요한 시험.**

    저장을 안 하므로 304 가 서버가 가진 유일한 절약이다. 이게 깨지면 같은 문장을
    누를 때마다 **재합성하고 재과금**하는데, 아무 로그도 안 남아 조용히 샌다.
    """
    _seed(session_factory)
    c = TestClient(_build_app(session_factory))

    first = c.post("/api/v1/tts/speech", json={"text": "안녕"}, headers=_hdr())
    etag = first.headers["ETag"]
    assert len(spy) == 1

    again = c.post("/api/v1/tts/speech", json={"text": "안녕"},
                   headers=_hdr(**{"If-None-Match": etag}))

    assert again.status_code == 304
    assert len(spy) == 1, "304 인데 재합성했다 — 절약이 통째로 무효다"
    assert again.content == b"", "304 는 바디가 없어야 한다"


def test_etag_changes_when_the_voice_changes(session_factory, spy):
    """⚠ 같은 문장이라도 캐릭터가 바뀌면 **다른 소리**다 — ETag 도 달라야 한다.

    안 그러면 A 목소리로 받은 캐시가 B 목소리 요청에 적중해 엉뚱한 소리가 나온다.
    """
    ids = _seed(session_factory)
    c = TestClient(_build_app(session_factory))

    a = c.post("/api/v1/tts/speech", json={"text": "안녕"}, headers=_hdr())
    b = c.post("/api/v1/tts/speech",
               json={"text": "안녕", "character_id": ids["other_id"]}, headers=_hdr())

    assert a.headers["ETag"] != b.headers["ETag"]
