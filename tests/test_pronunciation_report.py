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
import domains.learning.service.pronunciation_report_service as report_module
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
    # ⭐ Q8(2026-09-24) — call_date(원시 UTC)·call_id 가 이력 행 그대로 실린다.
    assert [s["call_id"] for s in b["sessions"]] == [1, 2, 3]
    assert b["sessions"][0]["call_date"] == "2026-07-15T00:00:00Z"


# --------------------------------------------------------------------------- #
# R5-a(2026-09-24, bt-back) — 현지인 표현 짝(kind="native", C9)이 통과수 분모·
# 분자를 2배로 부풀리면 안 된다(앱이 보정할 수 없는 서버 계산값이라 서버가
# 맞게 줘야 한다). 짝은 목록·평균엔 그대로 남는다.
# --------------------------------------------------------------------------- #
def _fake_report_with_pairs(call_id: int):
    return PronunciationReport(
        call_id=call_id,
        country="United States",
        sentences=[
            # 기본 3개 — 2개 통과(98·92), 1개 탈락(71).
            PronSentenceScore(sentence_id=1, korean_sentence="문장A", total_score=98, pronunciation=98, fluency=95, rhythm=97),
            PronSentenceScore(sentence_id=11, korean_sentence="짝A", total_score=99, pronunciation=99, fluency=97, rhythm=98, kind="native"),
            PronSentenceScore(sentence_id=2, korean_sentence="문장B", total_score=71, pronunciation=71, fluency=88, rhythm=84),
            PronSentenceScore(sentence_id=12, korean_sentence="짝B", total_score=60, pronunciation=60, fluency=70, rhythm=65, kind="native"),
            PronSentenceScore(sentence_id=3, korean_sentence="문장C", total_score=92, pronunciation=94, fluency=90, rhythm=92),
            PronSentenceScore(sentence_id=13, korean_sentence="짝C", total_score=100, pronunciation=100, fluency=100, rhythm=100, kind="native"),
        ],
        sounds=[],
        comment="",
    )


def test_report_excludes_native_pairs_from_the_pass_count(session_factory, seeded, monkeypatch):
    """⛔⛔ 핵심 재현·수정 확인 — 기본 3 + 짝 3, 기본 2개 통과 → 「3개 중 2개」
    (짝을 세면 「6개 중 2개」로 잘못 뜬다)."""
    async def _report(**kw):
        return _fake_report_with_pairs(kw["call_id"])
    monkeypatch.setattr(pron_module, "get_pronunciation_report", _report)
    monkeypatch.setattr(pron_module, "get_pronunciation_history", _fake_history)

    client = TestClient(_build_app(session_factory))
    r = client.get(f"/api/v1/calls/{seeded['call']}/pronunciation-report", headers=_hdr())
    assert r.status_code == 200, r.text
    b = r.json()

    assert b["total"] == 3, "짝이 분모에 섞였다"
    assert b["passed"] == 2, "짝이 분자에 섞였다"

    # 짝도 여전히 목록에 나온다(전부 6개) — 세는 기준만 바뀐다.
    assert len(b["sentences"]) == 6, "짝의 점수가 목록에서 빠졌다"
    kinds = [s.get("kind") for s in b["sentences"]]
    assert kinds.count("native") == 3
    # 기본 문장은 kind 키 자체가 생략된다(진행규칙 5).
    base_rows = [s for s in b["sentences"] if "짝" not in s["sentence"]]
    assert all("kind" not in s for s in base_rows), "기본 문장에 kind 키가 남아 있다(키 생략 규약 위반)"

    # overall 평균은 짝을 포함한다(/result 의 ScoreAverage 와 같은 규칙 — call_service
    # .get_call_result 의 average 는 kind 로 안 거른다).
    all_totals = [98, 99, 71, 60, 92, 100]
    assert b["overall"] == round(sum(all_totals) / len(all_totals))


def test_report_unknown_call_404(session_factory, seeded, monkeypatch):
    async def _none(**kw):
        return None
    monkeypatch.setattr(pron_module, "get_pronunciation_report", _none)
    client = TestClient(_build_app(session_factory))
    r = client.get(f"/api/v1/calls/{seeded['call']}/pronunciation-report", headers=_hdr())
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Q9(2026-09-24, bt-back 운영 실측 member_id=88) — 「점수 없음」(발음 챌린지를 안
# 누른 통화, h.score is None)과 「0점」을 같은 값으로 뭉개면 delta 가 없는 하락을
# 만든다. score=None 은 키를 생략하지 않는다(진행규칙 5 대상이 아니다) — 값이
# 없다는 사실 자체를 앱에 알려야 하는 필드라서다.
# --------------------------------------------------------------------------- #
def _hist(call_id, score, sentence_count=5, call_date=None):
    return PronHistoryItem(
        call_id=call_id,
        call_date=call_date or datetime(2026, 7, 15 + call_id, tzinfo=timezone.utc),
        sentence_count=sentence_count,
        score=score,
    )


def test_score_less_session_reports_none_and_keeps_the_key():
    """⛔⛔ 핵심 재현·수정 확인 — 점수 없는 세션은 score=None 으로 나가고, 그
    None 이 진행규칙 5(kind·nuance 류) 처럼 키 자체를 빼는 대상이 **아니다**."""
    history = [_hist(1, None)]  # get_pronunciation_history 계약: 최신순(여기 1건뿐)
    out = report_module._sessions_from_history(history)
    assert len(out) == 1
    assert out[0].score is None
    dumped = out[0].model_dump()
    assert "score" in dumped and dumped["score"] is None, "score 키 자체가 생략됐다"


def test_score_gaps_never_produce_a_fabricated_drop():
    """⛔⛔ bt-back 운영 실측(member_id=88) 배열을 그대로 재현한다: 없음·없음·96·
    없음·없음(오래된순). 옛 코드는 0 으로 뭉개 96 다음 delta=-96(없는 하락)을
    냈다 — 이제 delta 가 한 번도 음수가 되면 안 된다."""
    history_newest_first = [
        _hist(5, None), _hist(4, None), _hist(3, 96), _hist(2, None), _hist(1, None),
    ]  # get_pronunciation_history 계약: 최신순으로 준다
    out = report_module._sessions_from_history(history_newest_first)
    scores = [s.score for s in out]
    deltas = [s.delta for s in out]
    assert scores == [None, None, 96, None, None]
    assert all(d is None or d >= 0 for d in deltas), f"음수 delta(없는 하락)가 나왔다: {deltas}"
    # 96점 세션 자체는 "직전 점수 있는 세션"이 없어 delta=None(비교 상대 없음 — "—").
    assert deltas[2] is None
    # 96 다음의 점수 없는 세션들은 자기 점수가 없으니 delta 자체가 None 이다.
    assert deltas[3] is None and deltas[4] is None


def test_prev_score_is_not_overwritten_by_a_scoreless_session():
    """⛔⛔ bt-back «제일 틀리기 쉽다» 는 아니지만 명시적으로 지적한 자리 — 점수
    없는 세션이 «직전 점수 있는 세션» 과의 비교 사슬을 끊으면 안 된다. 96 →
    없음 → 82(다음 점수 있는 통화)면 82 의 delta 는 **직전 없음(0) 대비가 아니라
    96 대비**(82-96=-14)여야 한다."""
    history_newest_first = [_hist(3, 82), _hist(2, None), _hist(1, 96)]
    out = report_module._sessions_from_history(history_newest_first)
    scores = [s.score for s in out]
    deltas = [s.delta for s in out]
    assert scores == [96, None, 82]
    assert deltas == [None, None, -14], \
        "점수 없는 세션이 prev 를 0 으로 덮어 82 의 delta 가 82(96 대비가 아니라 0 대비)로 나왔다"


def test_consecutive_real_scores_regress_unchanged():
    """정상 경로 회귀 — 점수가 연속으로 있으면 종전과 같은 delta 산수."""
    history_newest_first = [_hist(3, 90), _hist(2, 85), _hist(1, 80)]
    out = report_module._sessions_from_history(history_newest_first)
    assert [s.score for s in out] == [80, 85, 90]
    assert [s.delta for s in out] == [None, 5, 5]
