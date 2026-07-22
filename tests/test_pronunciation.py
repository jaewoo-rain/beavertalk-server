"""발음 상세(T8)·최근5 이력(T9) 엔드포인트 결정적 테스트 (외부 의존 0).

검증 대상:
    GET /api/v1/calls/{call_id}/pronunciation      (async, LLM 목)
    GET /api/v1/calls/pronunciation-history         (sync)

- 소리 집계: alpha 버킷·passes(>=80)·counted만·문장별 마지막 복습.
- 문장별 점수: 미복습(평가 없음) → null.
- 코칭 한마디: counted 수 기반 캐시(pron_feedback_n) 적중, 자모없음 None, country null 분기.
- 이력: 활성 문장수·counted 문장 평균.
- 404: 없거나 타인 통화.

LLM(gemini_analysis.generate_structured)은 레코더로 모킹(네트워크 0). 인증은 스텁.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Integer, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from core.config import settings as app_settings
from core.supabase_auth import AuthUser
from domains.account.models.member import Member
from domains.account.models.speak_country import SpeakCountry
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.evaluation import Evaluation
from domains.learning.models.review import Review
from domains.learning.models.sentence import Sentence

import core.deps as deps
import domains.learning.service.pronunciation_service as psvc


# ── 인증 스텁: Bearer == auth uuid("auth-*" 만 유효) ──────────────────────── #
def _fake_verify(token):
    if token and token.startswith("auth-"):
        return AuthUser(uid=token, email=f"{token}@test.io")
    return None


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(deps, "verify_token", _fake_verify)


# ── LLM 목: PronunciationTip 반환 + 호출 기록 ─────────────────────────────── #
class _LLMRecorder:
    def __init__(self, explanation="'ㄹ' 소리는 많은 분들이 헷갈려요, 괜찮아요."):
        self.calls: list[dict] = []
        self.explanation = explanation

    async def __call__(self, client, model, *, system_instruction, prompt, schema, **kw):
        self.calls.append({"prompt": prompt, "system": system_instruction, "model": model})
        return schema(explanation=self.explanation)


@pytest.fixture()
def llm(monkeypatch):
    rec = _LLMRecorder()
    monkeypatch.setattr(psvc.gemini_analysis, "generate_structured", rec)
    return rec


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


def _build_app(session_factory, *, genai_client=object()):
    from main import create_app
    app = create_app()
    app.state.session_factory = session_factory
    app.state.settings = app_settings
    app.state.genai_client = genai_client
    return app


def _hdr(auth="auth-member"):
    return {"Authorization": f"Bearer {auth}"}


def _phon(alpha, pron):
    return {"phoneme": alpha, "alpha": alpha, "pronunciation": pron}


@pytest.fixture()
def seeded(session_factory):
    """회원(+타인) / 통화 1건(done,normal) / 문장 3개(복습 유·무 혼합) 시드."""
    db = session_factory()
    try:
        voice = Voice(name="Fenrir", gender="male")
        db.add(voice)
        db.flush()
        ch = Character(name="비비", role="선생님", personality="다정",
                       voice_id=voice.voice_id, price=0)
        db.add(ch)
        db.flush()
        country = SpeakCountry(first_country="미국")
        db.add(country)
        db.flush()
        member = Member(language="en", korean_level=1, onboarding_completed=True,
                        auth_user_id="auth-member", speak_country_id=country.speak_country_id)
        other = Member(language="en", auth_user_id="auth-other")
        db.add_all([member, other])
        db.flush()
        call = Call(member_id=member.member_id, character_id=ch.character_id,
                    status="done", call_type="normal")
        db.add(call)
        db.flush()

        # 문장1: 복습됨(evaluation + counted 복습 2건 — 마지막만 집계).
        s1 = Sentence(call_id=call.call_id, korean_sentence="안녕하세요",
                      native_sentence="hi", locale="en",
                      evaluation=Evaluation(total_score=70, pronunciation=60,
                                            fluency=75, rhythm=80))
        db.add(s1)
        db.flush()
        # 오래된 counted(집계 제외돼야 함) — ㄹ 100.
        db.add(Review(sentence_id=s1.sentence_id, counted=True,
                      feedback={"phonemes": [_phon("ㄹ", 100)]}))
        db.flush()
        # 최신 counted(집계 대상) — ㄹ 40(fail), ㅏ 90(pass).
        db.add(Review(sentence_id=s1.sentence_id, counted=True,
                      feedback={"phonemes": [_phon("ㄹ", 40), _phon("ㅏ", 90)]}))
        # counted=False(집계 제외) — ㄹ 100 이지만 무시돼야.
        db.add(Review(sentence_id=s1.sentence_id, counted=False,
                      feedback={"phonemes": [_phon("ㄹ", 100)]}))

        # 문장2: 복습됨 — ㅏ 70(fail<80).
        s2 = Sentence(call_id=call.call_id, korean_sentence="반갑습니다",
                      native_sentence="nice", locale="en",
                      evaluation=Evaluation(total_score=50, pronunciation=50,
                                            fluency=50, rhythm=50))
        db.add(s2)
        db.flush()
        db.add(Review(sentence_id=s2.sentence_id, counted=True,
                      feedback={"phonemes": [_phon("ㅏ", 70)]}))

        # 문장3: 미복습(evaluation 없음) → 점수 null, 집계 무기여.
        s3 = Sentence(call_id=call.call_id, korean_sentence="고맙습니다",
                      native_sentence="thanks", locale="en")
        db.add(s3)

        db.commit()
        return {
            "member_id": member.member_id,
            "call_id": call.call_id,
            "s1": s1.sentence_id, "s2": s2.sentence_id, "s3": s3.sentence_id,
            "country_id": country.speak_country_id,
        }
    finally:
        db.close()


# ── T8: 소리 집계 ─────────────────────────────────────────────────────────── #
def test_sound_aggregate_last_counted_only(session_factory, seeded, llm):
    """alpha 버킷·passes(>=80)·counted만·문장별 마지막 복습만 산입."""
    app = _build_app(session_factory)
    client = TestClient(app)
    r = client.get(f"/api/v1/calls/{seeded['call_id']}/pronunciation", headers=_hdr())
    assert r.status_code == 200, r.text
    body = r.json()

    sounds = {s["alpha"]: s for s in body["sounds"]}
    # ㄹ: 문장1 최신 복습(40)만 — 오래된(100)·counted=False(100) 제외.
    assert sounds["ㄹ"]["attempts"] == 1
    assert sounds["ㄹ"]["passes"] == 0
    assert sounds["ㄹ"]["pronunciation_avg"] == 40.0
    # ㅏ: 문장1(90 pass) + 문장2(70 fail) = 2회, pass 1회, 평균 80.0.
    assert sounds["ㅏ"]["attempts"] == 2
    assert sounds["ㅏ"]["passes"] == 1
    assert sounds["ㅏ"]["pronunciation_avg"] == 80.0


def test_sentence_scores_unreviewed_null(session_factory, seeded, llm):
    """문장별=모든 활성 문장. 미복습 문장(s3)은 점수 전부 null."""
    app = _build_app(session_factory)
    client = TestClient(app)
    r = client.get(f"/api/v1/calls/{seeded['call_id']}/pronunciation", headers=_hdr())
    body = r.json()
    by_id = {s["sentence_id"]: s for s in body["sentences"]}
    assert len(body["sentences"]) == 3
    assert by_id[seeded["s1"]]["total_score"] == 70
    assert by_id[seeded["s3"]]["total_score"] is None
    assert by_id[seeded["s3"]]["pronunciation"] is None


# ── T8: 코칭 한마디(LLM/캐시/분기) ───────────────────────────────────────── #
def test_comment_worst_alpha_and_cache_hit(session_factory, seeded, llm):
    """최저 avg alpha(ㄹ 40)로 LLM 1콜 → 캐시(pron_feedback_n) 저장, 재요청은 미호출."""
    app = _build_app(session_factory)
    client = TestClient(app)
    cid = seeded["call_id"]

    r1 = client.get(f"/api/v1/calls/{cid}/pronunciation", headers=_hdr())
    assert r1.json()["comment"] == llm.explanation
    assert len(llm.calls) == 1
    assert "ㄹ" in llm.calls[0]["prompt"]      # 최저 avg 자모
    assert "미국" in llm.calls[0]["prompt"]     # country 포함

    # 캐시 저장 확인.
    db = session_factory()
    try:
        call = db.get(Call, cid)
        assert call.pron_feedback == llm.explanation
        assert call.pron_feedback_n == 3        # counted 복습 총 3건(s1 2 + s2 1)
    finally:
        db.close()

    # 재요청: counted 수 불변 → LLM 미호출, 캐시 재사용.
    r2 = client.get(f"/api/v1/calls/{cid}/pronunciation", headers=_hdr())
    assert r2.json()["comment"] == llm.explanation
    assert len(llm.calls) == 1                  # 추가 호출 없음


def test_comment_none_when_no_phonemes(session_factory, seeded, llm):
    """자모 없음(집계 빈) → comment=None, LLM 스킵."""
    # s1/s2 의 복습 feedback 에서 phonemes 제거.
    db = session_factory()
    try:
        for rv in db.scalars(select(Review)).all():
            rv.feedback = {"phonemes": []}
        db.commit()
    finally:
        db.close()

    app = _build_app(session_factory)
    client = TestClient(app)
    r = client.get(f"/api/v1/calls/{seeded['call_id']}/pronunciation", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["sounds"] == []
    assert r.json()["comment"] is None
    assert len(llm.calls) == 0


def test_comment_country_null_branch(session_factory, seeded, llm):
    """speak_country null → country 없이 소리만 설명(프롬프트에 국가명 미포함)."""
    db = session_factory()
    try:
        m = db.get(Member, seeded["member_id"])
        m.speak_country_id = None
        db.commit()
    finally:
        db.close()

    app = _build_app(session_factory)
    client = TestClient(app)
    r = client.get(f"/api/v1/calls/{seeded['call_id']}/pronunciation", headers=_hdr())
    body = r.json()
    assert body["country"] is None
    assert body["comment"] == llm.explanation
    assert len(llm.calls) == 1
    assert "미국" not in llm.calls[0]["prompt"]  # 국가명 미포함
    assert "많은 학습자" in llm.calls[0]["prompt"]


def test_comment_graceful_when_client_none(session_factory, seeded):
    """genai client None → LLM 스킵, 기존 캐시(없으면 None)로 폴백. 나머지 응답 정상."""
    app = _build_app(session_factory, genai_client=None)
    client = TestClient(app)
    r = client.get(f"/api/v1/calls/{seeded['call_id']}/pronunciation", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["comment"] is None
    assert len(r.json()["sounds"]) == 2         # 집계는 정상


def test_pronunciation_404_other_and_missing(session_factory, seeded, llm):
    """타인 통화·없는 통화 → 404."""
    app = _build_app(session_factory)
    client = TestClient(app)
    # 타인 소유 통화.
    r_other = client.get(
        f"/api/v1/calls/{seeded['call_id']}/pronunciation", headers=_hdr("auth-other")
    )
    assert r_other.status_code == 404
    # 없는 통화.
    r_missing = client.get("/api/v1/calls/999999/pronunciation", headers=_hdr())
    assert r_missing.status_code == 404


# ── T9: 최근5 이력 ────────────────────────────────────────────────────────── #
def test_history_counts_and_avg(session_factory, seeded, llm):
    """활성 문장수=3, 점수=counted 문장 평균(s1 70·s2 50 → 60.0; s3 제외)."""
    app = _build_app(session_factory)
    client = TestClient(app)
    r = client.get("/api/v1/calls/pronunciation-history", headers=_hdr())
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 1
    item = items[0]
    assert item["call_id"] == seeded["call_id"]
    assert item["sentence_count"] == 3
    assert item["score"] == 60.0


def test_history_empty_when_no_counted_reviews(session_factory, seeded, llm):
    """counted 문장이 없으면 score=None(문장수는 유지)."""
    db = session_factory()
    try:
        for rv in db.scalars(select(Review)).all():
            rv.counted = False
        db.commit()
    finally:
        db.close()

    app = _build_app(session_factory)
    client = TestClient(app)
    r = client.get("/api/v1/calls/pronunciation-history", headers=_hdr())
    item = r.json()[0]
    assert item["sentence_count"] == 3
    assert item["score"] is None
