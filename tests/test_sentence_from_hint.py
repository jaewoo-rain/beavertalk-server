# -*- coding: utf-8 -*-
"""통화 중 힌트를 즐겨찾기에 담는다(`POST /api/v1/sentences/from-hint`).

## 이 기능이 지키는 것 두 개 — 둘 다 **조용히 틀리는** 종류다

1. **중복은 에러가 아니라 재사용이다.** 🔖 는 연타·재진입이 흔하다. 막지 않으면 목록이
   같은 문장으로 더러워지는데, 더러워질 때까지 아무도 모른다.
2. **남의 `call_id` 로는 못 담는다.** 아무도 안 눌러 보면 영영 모르는 구멍이다.

## ⚠ 왜 힌트가 뜰 때 저장하지 않나
사장님: "즐겨찾기 안해도 DB저장되면 너무 낭비인데?" — 5분 통화에 힌트 5회면 15행이
쌓이는데 그 대부분을 아무도 안 담는다.
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
from domains.learning.models.sentence import Sentence

import core.deps as deps

URL = "/api/v1/sentences/from-hint"


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


def _seed(session_factory, *, language="en"):
    """내 통화 1건 + **남의 통화** 1건."""
    db = session_factory()
    try:
        v = Voice(name="Leda", gender="female")
        db.add(v)
        db.flush()
        ch = Character(name="바바", role="선생님", personality="시크",
                       voice_id=v.voice_id, price=0)
        db.add(ch)
        db.flush()
        me = Member(language=language, korean_level=1, onboarding_completed=True,
                    auth_user_id="auth-member")
        other = Member(language="en", korean_level=1, onboarding_completed=True,
                       auth_user_id="auth-other")
        db.add_all([me, other])
        db.flush()
        mine = Call(member_id=me.member_id, character_id=ch.character_id,
                    status="done", call_type="normal")
        theirs = Call(member_id=other.member_id, character_id=ch.character_id,
                      status="done", call_type="normal")
        db.add_all([mine, theirs])
        db.commit()
        return {"call_id": mine.call_id, "other_call_id": theirs.call_id,
                "member_id": me.member_id}
    finally:
        db.close()


def _build_app(session_factory):
    from main import create_app
    app = create_app()
    app.state.session_factory = session_factory
    app.state.settings = app_settings
    app.state.genai_client = object()
    return app


def _hdr():
    return {"Authorization": "Bearer auth-member"}


def _rows(session_factory, call_id):
    db = session_factory()
    try:
        return db.query(Sentence).filter(Sentence.call_id == call_id).all()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 1. 기능 자체
# --------------------------------------------------------------------------- #

def test_bookmarking_a_hint_creates_one_bookmarked_row(session_factory):
    ids = _seed(session_factory)
    c = TestClient(_build_app(session_factory))

    r = c.post(URL, json={"call_id": ids["call_id"], "korean": "화장실이 어디예요?",
                          "native": "Where is the restroom?"}, headers=_hdr())

    assert r.status_code == 200
    body = r.json()
    assert body["is_bookmarked"] is True
    assert body["korean_sentence"] == "화장실이 어디예요?"
    assert body["native_sentence"] == "Where is the restroom?"

    rows = _rows(session_factory, ids["call_id"])
    assert len(rows) == 1
    assert rows[0].source_type == "hint", "출처 표식이 없으면 나중에 되짚을 수 없다"


def test_locale_comes_from_the_member_not_the_client(session_factory):
    """⭐ `locale` 은 클라가 안 보낸다 — 서버가 회원에서 뽑는다.

    ⚠ 통화 분석과 **같은 함수**(`_base_locale`)를 써야 한다. 'ko-KR' 을 날것으로 넣으면
      같은 회원의 문장인데 표기가 갈린다.
    """
    ids = _seed(session_factory, language="ko-KR")
    c = TestClient(_build_app(session_factory))

    r = c.post(URL, json={"call_id": ids["call_id"], "korean": "안녕", "native": "hi"},
               headers=_hdr())

    assert r.json()["locale"] == "ko", "지역 코드가 정규화되지 않았다"


# --------------------------------------------------------------------------- #
# 2. ⭐ 중복은 에러가 아니라 재사용이다
# --------------------------------------------------------------------------- #

def test_bookmarking_the_same_hint_twice_reuses_the_row(session_factory):
    """⛔ 🔖 는 연타·재진입이 흔하다. 두 번째도 **200 에 같은 id** 여야 한다.

    실패로 다루면 프론트가 에러를 띄우고, 새 행을 만들면 목록이 같은 문장으로 더러워진다.
    """
    ids = _seed(session_factory)
    c = TestClient(_build_app(session_factory))
    body = {"call_id": ids["call_id"], "korean": "안녕", "native": "hi"}

    a = c.post(URL, json=body, headers=_hdr())
    b = c.post(URL, json=body, headers=_hdr())

    assert a.status_code == b.status_code == 200
    assert a.json()["sentence_id"] == b.json()["sentence_id"]
    assert len(_rows(session_factory, ids["call_id"])) == 1


def test_rebookmarking_after_unbookmark_flips_it_back(session_factory):
    """담았다 뺐다 다시 담으면 **같은 행**이 다시 켜진다(새 행이 안 생긴다)."""
    ids = _seed(session_factory)
    c = TestClient(_build_app(session_factory))
    body = {"call_id": ids["call_id"], "korean": "안녕", "native": "hi"}

    sid = c.post(URL, json=body, headers=_hdr()).json()["sentence_id"]
    c.patch(f"/api/v1/sentences/{sid}/bookmark", json={"is_bookmarked": False},
            headers=_hdr())

    again = c.post(URL, json=body, headers=_hdr())

    assert again.json()["sentence_id"] == sid
    assert again.json()["is_bookmarked"] is True
    assert len(_rows(session_factory, ids["call_id"])) == 1


def test_a_different_sentence_in_the_same_call_makes_a_new_row(session_factory):
    """중복 판정이 (call_id, korean) 이다 — 다른 문장은 당연히 새 행이다."""
    ids = _seed(session_factory)
    c = TestClient(_build_app(session_factory))

    c.post(URL, json={"call_id": ids["call_id"], "korean": "안녕", "native": "hi"},
           headers=_hdr())
    c.post(URL, json={"call_id": ids["call_id"], "korean": "고마워", "native": "thanks"},
           headers=_hdr())

    assert len(_rows(session_factory, ids["call_id"])) == 2


# --------------------------------------------------------------------------- #
# 3. ⭐ 소유 검증 — 아무도 안 눌러 보면 영영 모르는 구멍
# --------------------------------------------------------------------------- #

def test_cannot_plant_a_sentence_in_someone_elses_call(session_factory):
    """⛔ 남의 `call_id` 로는 못 담는다. **404 다**(403 아님).

    "남의 것"이라고 알려 주면 그 통화의 존재가 새어 나간다 — `_get_owned` 와 같은 규율.
    """
    ids = _seed(session_factory)
    c = TestClient(_build_app(session_factory))

    r = c.post(URL, json={"call_id": ids["other_call_id"], "korean": "침입",
                          "native": "intrusion"}, headers=_hdr())

    assert r.status_code == 404
    assert _rows(session_factory, ids["other_call_id"]) == [], "남의 통화에 행이 생겼다"


def test_unknown_call_is_404(session_factory):
    _seed(session_factory)
    c = TestClient(_build_app(session_factory))

    r = c.post(URL, json={"call_id": 99999, "korean": "안녕", "native": "hi"},
               headers=_hdr())

    assert r.status_code == 404


def test_anonymous_is_401(session_factory):
    ids = _seed(session_factory)
    c = TestClient(_build_app(session_factory))

    r = c.post(URL, json={"call_id": ids["call_id"], "korean": "안녕", "native": "hi"})

    assert r.status_code == 401


@pytest.mark.parametrize("bad", [
    {"call_id": 1, "korean": "", "native": "hi"},
    {"call_id": 1, "korean": "안녕"},                 # native 누락
    {"korean": "안녕", "native": "hi"},               # call_id 누락
])
def test_bad_payload_is_422(session_factory, bad):
    _seed(session_factory)
    c = TestClient(_build_app(session_factory))

    assert c.post(URL, json=bad, headers=_hdr()).status_code == 422


# --------------------------------------------------------------------------- #
# 4. 담긴 문장은 기존 즐겨찾기와 **같은 행**이다
# --------------------------------------------------------------------------- #

def test_the_saved_hint_behaves_like_any_other_bookmark(session_factory):
    """⭐ 특별 취급이 필요한 곳이 없다는 것을 못박는다.

    `Sentence` 는 `call_id` 만 필수라, 담긴 힌트는 분석이 만든 행과 `source_type` 값
    하나만 다르다. ⇒ 즐겨찾기 목록·해제가 그대로 돈다.
    """
    ids = _seed(session_factory)
    c = TestClient(_build_app(session_factory))

    sid = c.post(URL, json={"call_id": ids["call_id"], "korean": "안녕", "native": "hi"},
                 headers=_hdr()).json()["sentence_id"]

    listed = c.get("/api/v1/members/me/bookmarks", headers=_hdr())
    assert listed.status_code == 200
    assert sid in [x["sentence_id"] for x in listed.json()], "즐겨찾기 목록에 안 나온다"

    off = c.patch(f"/api/v1/sentences/{sid}/bookmark",
                  json={"is_bookmarked": False}, headers=_hdr())
    assert off.status_code == 200 and off.json()["is_bookmarked"] is False
