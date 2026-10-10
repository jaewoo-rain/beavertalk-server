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


def test_report_counts_base_and_native_pairs_together(session_factory, seeded, monkeypatch):
    """PM-DEC-476: 승인 ca5bb1c/465의 기본·현지인 전체 집계 계약을 유지한다.

    기본 3개(통과 2) + 현지인 3개(통과 2) → 전체 6개 중 4개 통과.
    """
    async def _report(**kw):
        return _fake_report_with_pairs(kw["call_id"])
    monkeypatch.setattr(pron_module, "get_pronunciation_report", _report)
    monkeypatch.setattr(pron_module, "get_pronunciation_history", _fake_history)

    client = TestClient(_build_app(session_factory))
    r = client.get(f"/api/v1/calls/{seeded['call']}/pronunciation-report", headers=_hdr())
    assert r.status_code == 200, r.text
    b = r.json()

    assert b["total"] == 6, "전체 활성 학습 문장을 분모에 포함해야 한다"
    assert b["passed"] == 4, "기본·현지인 문장의 80점 이상 결과를 함께 세어야 한다"

    # 전체 목록·kind·기본 kind 생략·전체 평균 계약은 유지한다.
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


# --------------------------------------------------------------------------- #
# §2 정정(2026-09-27, 앱 요청) — 세 번째 「미복습=0」 지점(bt-back 확인·승인).
# --------------------------------------------------------------------------- #
def _fake_report_with_unreviewed(call_id: int):
    return PronunciationReport(
        call_id=call_id,
        country="United States",
        sentences=[
            PronSentenceScore(sentence_id=1, korean_sentence="문장A", total_score=90,
                              pronunciation=90, fluency=88, rhythm=85),
            # 미복습(placeholder Evaluation) — 네 칸 전부 None. 운영 2,870/2,956 이 이 모양.
            PronSentenceScore(sentence_id=2, korean_sentence="문장B"),
        ],
        sounds=[],
        comment="",
    )


def test_unreviewed_sentence_reports_null_not_zero(session_factory, seeded, monkeypatch):
    """⛔⛔ 핵심 — 미복습 문장(pronunciation/fluency/rhythm 전부 None)이 0 이 아니라
    null 로 나가고, 키가 생략되지 않는다(kind 와 다른 규약 — 값이 없다는 사실 자체를
    앱에 알려야 한다)."""
    async def _report(**kw):
        return _fake_report_with_unreviewed(kw["call_id"])
    monkeypatch.setattr(pron_module, "get_pronunciation_report", _report)
    monkeypatch.setattr(pron_module, "get_pronunciation_history", _fake_history)

    client = TestClient(_build_app(session_factory))
    r = client.get(f"/api/v1/calls/{seeded['call']}/pronunciation-report", headers=_hdr())
    assert r.status_code == 200, r.text
    b = r.json()

    reviewed, unreviewed = b["sentences"]
    assert reviewed["pronunciation"] == 90
    assert unreviewed["pronunciation"] is None
    assert unreviewed["fluency"] is None
    assert unreviewed["rhythm"] is None
    # ⛔ 키 자체는 남아 있어야 한다(0 으로 뭉개지도, 키가 생략되지도 않는다).
    assert "pronunciation" in unreviewed and "fluency" in unreviewed and "rhythm" in unreviewed
    # 평균(overall 등)은 미복습 문장을 이미 걸러서 계산한다(회귀 — 기존 _avg 로직 그대로).
    assert b["pronunciation"] == 90


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


# --------------------------------------------------------------------------- #
# PM-DEC-333/337/341(2026-10-03, 앱 요청) — `retry_sounds`(「다시 해볼 소리」).
#
# 선정 규칙 1~8 을 **서버가** 거른다. 규칙 → 시험 대응:
#   1 이 통화 문장별 마지막 counted 복습만  → test_retry_attempts_are_call_scoped
#   2 sound_key None 제외                  → test_retry_drops_sounds_without_a_key
#   3 과(sound_lesson) 있는 소리만          → test_retry_drops_sounds_without_a_lesson
#   4 misses >= 2                          → test_retry_requires_two_misses
#   5 misses↓ → 정확도↑ → sound_key        → test_retry_sort_is_three_tiered
#   6 최대 3개                             → test_retry_caps_at_three
#   7 스텁 채점 제외                       → test_retry_score_ignores_uncounted_reviews
#     (`attempts`/`misses` 쪽 규칙 7 은 이미 `tests/test_pronunciation.py` 의
#      `test_sound_aggregate_last_counted_only` 가 고정한다 — counted=False 복습은
#      `get_last_counted_reviews` 가 아예 넘기지 않는다)
#   8 0개면 카드 숨김 = 앱 몫              → test_retry_empty_list_when_nothing_qualifies
# + 점수가 /weak-sounds 와 같은 값          → test_retry_score_matches_weak_sounds_*
# + 하위호환                               → test_retry_sounds_absent_data_keeps_old_shape
# --------------------------------------------------------------------------- #
from domains.learning.models.member_sound_score import MemberSoundScore
from domains.learning.models.review import Review
from domains.learning.models.sentence import Sentence
from domains.learning.models.sound_lesson import SoundLesson
from domains.learning.models.sound_lesson_i18n import SoundLessonI18n


def _sound(sound_key, attempts, passes, *, alpha=None, avg=50.0):
    """이 통화의 소리 집계 1줄(= aggregate_sounds 출력 모양). misses = attempts-passes."""
    return SoundAggregate(
        alpha=alpha or (sound_key or "?"),
        attempts=attempts,
        passes=passes,
        pronunciation_avg=avg,
        sound_key=sound_key,
    )


def _report_with_sounds(call_id: int, sounds):
    """문장은 고정(통과수 회귀용), sounds 만 시험마다 바꾼다."""
    return PronunciationReport(
        call_id=call_id,
        country=None,
        sentences=[
            PronSentenceScore(sentence_id=1, korean_sentence="문장A", total_score=90,
                              pronunciation=90, fluency=88, rhythm=85),
        ],
        sounds=sounds,
        comment="",
    )


def _patch_report(monkeypatch, sounds):
    async def _report(**kw):
        return _report_with_sounds(kw["call_id"], sounds)
    monkeypatch.setattr(pron_module, "get_pronunciation_report", _report)
    monkeypatch.setattr(pron_module, "get_pronunciation_history", _fake_history)


def _lesson(sound_key, label, order, *, position=None, jamo=None):
    return SoundLesson(
        sound_key=sound_key, type="sound", label=label, position=position, jamo=jamo,
        diagram="Airflow / test", card_desc=f"{label} 소리 내는 법", sort_order=order,
        payload={"how_to": ["a"], "words": [], "sentence": {"text": "문장"},
                 "test": {"text": "평가"}},
    )


def _phon(alpha, pron, position):
    """실경로와 같은 모양 — core/speechsuper 가 position·sound_key 를 붙여 준다."""
    key = {"초성": f"onset_{alpha}", "종성": f"coda_{alpha}"}.get(position)
    return {"phoneme": alpha, "alpha": alpha, "pronunciation": pron,
            "position": position, "sound_key": key}


@pytest.fixture()
def lessons(session_factory, seeded):
    """과 마스터 4개 + `coda_ㄹ` 번역(es) — `retry_sounds` 가 볼 콘텐츠.

    ⛔ `coda_ㅍ` 은 **일부러 과를 만들지 않는다** — 규칙 3(과 없는 소리 제외)의 대상.
    """
    db = session_factory()
    try:
        db.add_all([
            _lesson("coda_ㄹ", "받침 ㄹ", 0, position="종성", jamo="ㄹ"),
            _lesson("onset_ㅊ", "초성 ㅊ", 1, position="초성", jamo="ㅊ"),
            _lesson("coda_ㄱ", "받침 ㄱ", 2, position="종성", jamo="ㄱ"),
            _lesson("coda_ㄴ", "받침 ㄴ", 3, position="종성", jamo="ㄴ"),
        ])
        db.add(SoundLessonI18n(sound_key="coda_ㄹ", locale="es", label="ㄹ final",
                               card_desc="Pon la punta de la lengua", payload={}))
        db.commit()
        return seeded
    finally:
        db.close()


@pytest.fixture()
def reviewed(session_factory, lessons):
    """통화에 실제 복습 2건(counted) — 점수의 출처(복습 집계)를 DB 에 만든다.

    집계: coda_ㄹ = (40+50)/2 = 45 · onset_ㅊ = (60+80)/2 = 70.
    ⛔ counted=False 복습 1건을 **섞어 둔다**(coda_ㄹ 100) — 규칙 7(스텁 제외)이
      점수 쪽에서도 지켜지는지 보려면 그게 무시돼야 한다(스텁 채점은 counted=False 다).
    """
    db = session_factory()
    try:
        s1 = Sentence(call_id=lessons["call"], korean_sentence="가",
                      native_sentence="a", locale="en")
        s2 = Sentence(call_id=lessons["call"], korean_sentence="나",
                      native_sentence="b", locale="en")
        db.add_all([s1, s2])
        db.flush()
        db.add(Review(sentence_id=s1.sentence_id, counted=True, feedback={"phonemes": [
            _phon("ㄹ", 40, "종성"), _phon("ㅊ", 60, "초성"),
        ]}))
        db.add(Review(sentence_id=s2.sentence_id, counted=True, feedback={"phonemes": [
            _phon("ㄹ", 50, "종성"), _phon("ㅊ", 80, "초성"),
        ]}))
        # 스텁 채점 자리 — 집계에 섞이면 coda_ㄹ 이 45 가 아니라 63 이 된다.
        db.add(Review(sentence_id=s2.sentence_id, counted=False, feedback={"phonemes": [
            _phon("ㄹ", 100, "종성"),
        ]}))
        db.commit()
        return lessons
    finally:
        db.close()


def _get_retry(client, call_id):
    r = client.get(f"/api/v1/calls/{call_id}/pronunciation-report", headers=_hdr())
    assert r.status_code == 200, r.text
    return r.json()["retry_sounds"]


# ── 규칙 2·3·4 ──────────────────────────────────────────────────────────── #
def test_retry_drops_sounds_without_a_key(session_factory, lessons, monkeypatch):
    """규칙 2 — `sound_key` 가 None 인 버킷(모음·위치 미부착 옛 복습)은 제외.

    misses 가 가장 많아도(8) 학습 단위로 맞출 수 없어 카드가 될 수 없다.
    """
    _patch_report(monkeypatch, [
        _sound(None, 9, 1, alpha="ㅏ"),          # misses 8 — 1순위처럼 보이지만 키가 없다
        _sound("coda_ㄹ", 7, 3),                 # misses 4
    ])
    out = _get_retry(TestClient(_build_app(session_factory)), lessons["call"])
    assert [c["sound_key"] for c in out] == ["coda_ㄹ"], "sound_key 없는 버킷이 올라왔다"


def test_retry_candidates_drop_none_keys_before_touching_db():
    """규칙 2 — 키 없는 버킷은 **후보 단계에서** 떨어진다(DB 를 보기 전에).

    ⛔ 돌연변이 검증에서 드러난 자리 — 이 필터를 지워도 응답은 같다(과 조회가
      `cards.get("")` 로 어차피 못 찾아 규칙 3 이 뒤에서 잡는다). 그래서 응답만 보는
      시험으로는 이 필터의 삭제가 안 잡힌다. 그런데 **비용이 다르다**: 키 없는 소리만
      있는 통화(모음만 틀린 통화)가 후보로 남으면 `_retry_cards` 가 DB 왕복 4~6번을
      헛돈다. 후보가 비어야 `_retry_cards` 가 즉시 반환한다 — 그 계약을 여기서 잡는다.
    """
    cands = report_module._retry_candidates([
        _sound(None, 9, 1, alpha="ㅏ"),
        _sound(None, 20, 2, alpha="ㅓ/ㅗ 구분"),
    ])
    assert cands == [], "키 없는 버킷이 후보로 남아 DB 왕복을 헛돈다"


def test_retry_drops_sounds_without_a_lesson(session_factory, lessons, monkeypatch):
    """규칙 3 — `sound_lesson` 에 과가 없는 소리는 제외(진입할 화면이 없다).

    ⛔ 핵심: 과 없는 소리가 **슬롯을 잡아먹지 않는다.** `coda_ㅍ`(misses 9, 1순위)가
      빠진 자리에 4순위까지 올라와 3개가 채워져야 한다 — 먼저 3개로 자르고 과를
      확인하는 구현이면 결과가 2개로 줄어든다.
    """
    _patch_report(monkeypatch, [
        _sound("coda_ㅍ", 10, 1),   # misses 9 — 과가 없다(일부러 시드 안 함)
        _sound("coda_ㄹ", 7, 3),    # misses 4
        _sound("onset_ㅊ", 8, 5),   # misses 3
        _sound("coda_ㄱ", 6, 4),    # misses 2
    ])
    out = _get_retry(TestClient(_build_app(session_factory)), lessons["call"])
    assert [c["sound_key"] for c in out] == ["coda_ㄹ", "onset_ㅊ", "coda_ㄱ"], \
        "과 없는 소리가 올라왔거나 그 소리가 슬롯을 잡아먹었다"


def test_retry_requires_two_misses(session_factory, lessons, monkeypatch):
    """규칙 4 — `misses >= RETRY_MIN_MISSES`(=2). 1번 틀린 소리는 카드가 아니다."""
    assert report_module.RETRY_MIN_MISSES == 2
    _patch_report(monkeypatch, [
        _sound("coda_ㄹ", 6, 4),    # misses 2 → 포함(경계값)
        _sound("onset_ㅊ", 9, 8),   # misses 1 → 제외(경계 바로 아래)
        _sound("coda_ㄱ", 5, 5),    # misses 0 → 제외
    ])
    out = _get_retry(TestClient(_build_app(session_factory)), lessons["call"])
    assert [c["sound_key"] for c in out] == ["coda_ㄹ"]
    assert out[0]["misses"] == 2


# ── 규칙 5 (정렬 3단) ────────────────────────────────────────────────────── #
def test_retry_sort_is_three_tiered():
    """규칙 5 — misses 내림차순 → 정확도(passes/attempts) 오름차순 → sound_key.

    3단 전부를 한 배열로 가른다(순수 함수라 DB 없이 본다).
    - misses 4 가 1순위(나머지는 전부 2).
    - misses 동률 3개 중 정확도 0.5·0.5·0.8 → 0.8 은 꼴찌.
    - 정확도까지 동률인 둘(coda_ㄱ·coda_ㄴ, 4회 중 2통과)은 sound_key 사전순.
    """
    cands = report_module._retry_candidates([
        _sound("coda_ㄴ", 4, 2),     # misses 2, 정확도 0.50
        _sound("onset_ㅊ", 10, 8),   # misses 2, 정확도 0.80
        _sound("coda_ㄱ", 4, 2),     # misses 2, 정확도 0.50 (ㄱ < ㄴ)
        _sound("coda_ㄹ", 7, 3),     # misses 4 → 1순위
    ])
    assert [s.sound_key for s in cands] == ["coda_ㄹ", "coda_ㄱ", "coda_ㄴ", "onset_ㅊ"]


def test_retry_sort_uses_real_ratio_not_rounded_accuracy():
    """규칙 5 의 2단 — 정확도는 **실수 비율**이다.

    둘 다 misses 1000 인데 정확도는 0.66667(2000/3000) vs 0.66678(2001/3001) 이다.
    `_accuracy` 처럼 정수로 반올림하면 **둘 다 67** 이 되어 동률 → 3단(sound_key
    사전순)이 순서를 정해 `coda_ㄱ` 이 앞선다. 실수 비율로 비교해야 더 못한
    `onset_ㅎ` 이 앞선다.
    ⚠ 키 선정 주의 — 더 못한 쪽이 사전순으로도 앞서면(`coda_…` vs `onset_…`) 두 구현이
      같은 답을 내서 이 시험이 아무것도 못 잡는다(실제로 한 번 그렇게 썼다가 돌연변이
      검증에서 걸렸다). 그래서 **못한 쪽을 사전순 뒤로** 둔다.
    """
    cands = report_module._retry_candidates([
        _sound("coda_ㄱ", 3001, 2001),    # 0.66678  — 더 잘했는데 사전순 앞
        _sound("onset_ㅎ", 3000, 2000),   # 0.66667  ← 더 못했다(사전순 뒤)
    ])
    assert [s.sound_key for s in cands] == ["onset_ㅎ", "coda_ㄱ"], \
        "정확도를 정수로 반올림해 비교했다(동률이 되어 sound_key 가 순서를 정했다)"


def test_retry_sort_survives_the_endpoint(session_factory, lessons, monkeypatch):
    """규칙 5 — 정렬이 응답까지 그대로 간다(조립 과정에서 순서가 뒤집히지 않는다).

    ⛔ 1단(misses)과 2단(정확도)을 **둘 다** 쓰는 배열이어야 한다 — misses 가 전부
      같은 배열로 쓰면 1단을 뒤집어도 답이 그대로라 이 시험이 아무것도 못 잡는다
      (돌연변이 검증에서 걸렸다). 3단은 순수 함수 시험이 본다.
    """
    _patch_report(monkeypatch, [
        _sound("onset_ㅊ", 10, 8),   # misses 2, 정확도 0.80
        _sound("coda_ㄱ", 4, 2),     # misses 2, 정확도 0.50
        _sound("coda_ㄹ", 7, 3),     # misses 4 → 1순위
    ])
    out = _get_retry(TestClient(_build_app(session_factory)), lessons["call"])
    assert [c["sound_key"] for c in out] == ["coda_ㄹ", "coda_ㄱ", "onset_ㅊ"]


# ── 규칙 6 (최대 3개) ───────────────────────────────────────────────────── #
def test_retry_caps_at_three(session_factory, lessons, monkeypatch):
    """규칙 6 — 자격 있는 소리가 4개여도 상위 3개만. 잘린 것은 꼴찌여야 한다."""
    assert report_module.RETRY_LIMIT == 3
    _patch_report(monkeypatch, [
        _sound("coda_ㄹ", 9, 1),     # misses 8
        _sound("onset_ㅊ", 8, 3),    # misses 5
        _sound("coda_ㄱ", 6, 3),     # misses 3
        _sound("coda_ㄴ", 5, 3),     # misses 2 → 잘린다
    ])
    out = _get_retry(TestClient(_build_app(session_factory)), lessons["call"])
    assert len(out) == 3
    assert [c["sound_key"] for c in out] == ["coda_ㄹ", "onset_ㅊ", "coda_ㄱ"]


# ── 규칙 8 (0개면 `[]`) ─────────────────────────────────────────────────── #
def test_retry_empty_list_when_nothing_qualifies(session_factory, lessons, monkeypatch):
    """규칙 8 — 서버는 `[]` 만 보낸다(카드 숨김은 앱 몫). 키를 빼지 않는다."""
    _patch_report(monkeypatch, [_sound("coda_ㄹ", 9, 8)])  # misses 1
    r = TestClient(_build_app(session_factory)).get(
        f"/api/v1/calls/{lessons['call']}/pronunciation-report", headers=_hdr()
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "retry_sounds" in body, "키 자체가 생략됐다(앱이 `[]` 를 보고 카드를 숨긴다)"
    assert body["retry_sounds"] == []


def test_retry_empty_when_no_sounds_at_all(session_factory, lessons, monkeypatch):
    """소리 집계가 비었으면(발음 챌린지를 안 누른 통화) 당연히 `[]` — 500 아님."""
    _patch_report(monkeypatch, [])
    out = _get_retry(TestClient(_build_app(session_factory)), lessons["call"])
    assert out == []


# ── 규칙 1 (이 통화 범위) ───────────────────────────────────────────────── #
def test_retry_attempts_are_call_scoped(session_factory, reviewed, monkeypatch):
    """규칙 1 — `attempts`/`misses` 는 **이 통화** 집계(`report.sounds`)다.

    DB 의 복습엔 coda_ㄹ 이 2번뿐인데(그래서 점수는 45) 이 통화 집계는 7번/4틀림이다.
    `attempts` 가 취약 발음 목록의 `WeakSoundItem.attempts`(학습 평가 제출 횟수)나
    회원 전체 집계로 바뀌면 이 시험이 깨진다.
    """
    _patch_report(monkeypatch, [_sound("coda_ㄹ", 7, 3)])
    out = _get_retry(TestClient(_build_app(session_factory)), reviewed["call"])
    assert len(out) == 1
    assert out[0]["attempts"] == 7 and out[0]["misses"] == 4
    assert out[0]["score"] == 45  # 점수만 복습 집계에서 온다(이 통화 7번과 무관)


# ── 점수가 /weak-sounds 와 같은 값 ──────────────────────────────────────── #
def test_retry_score_matches_weak_sounds_for_unlearned(session_factory, reviewed, monkeypatch):
    """⭐ 핵심 요구 — 미학습 소리의 점수가 취약 발음 목록과 **같은 값**이다.

    두 API 를 실제로 호출해 대조한다(산식 복제가 아니라 같은 코드를 지나는지 확인).
    """
    _patch_report(monkeypatch, [_sound("coda_ㄹ", 7, 3), _sound("onset_ㅊ", 8, 6)])
    client = TestClient(_build_app(session_factory))

    weak = client.get("/api/v1/pronunciation/weak-sounds", headers=_hdr()).json()
    by_key = {i["sound_key"]: i for i in weak["national"]["items"] + weak["mine"]}
    retry = {c["sound_key"]: c for c in _get_retry(client, reviewed["call"])}

    assert set(retry) == {"coda_ㄹ", "onset_ㅊ"}
    for key in retry:
        assert key in by_key, f"{key} 가 취약 발음 목록에 없어 대조를 못 했다"
        assert retry[key]["score"] == by_key[key]["score"], \
            f"{key} 점수가 두 화면에서 갈렸다: {retry[key]['score']} vs {by_key[key]['score']}"
    assert retry["coda_ㄹ"]["score"] == 45 and retry["onset_ㅊ"]["score"] == 70
    # 미학습이라 `learned=False` 인 소리다(= 집계 출처) — 대조가 학습 점수로 샌 게 아니다.
    assert by_key["coda_ㄹ"]["learned"] is False


def test_retry_score_matches_weak_sounds_for_learned(session_factory, reviewed, monkeypatch):
    """⭐ 핵심 요구 — 학습한 소리는 `member_sound_score.score` 가 집계를 덮고, 그
    덮인 값이 두 화면에서 같다(집계 45 가 아니라 학습 88)."""
    db = session_factory()
    try:
        db.add(MemberSoundScore(member_id=reviewed["member"], sound_key="coda_ㄹ",
                                score=88, best_score=88, attempts=3, baseline_score=45))
        db.commit()
    finally:
        db.close()

    _patch_report(monkeypatch, [_sound("coda_ㄹ", 7, 3)])
    client = TestClient(_build_app(session_factory))
    weak = client.get("/api/v1/pronunciation/weak-sounds", headers=_hdr()).json()
    by_key = {i["sound_key"]: i for i in weak["national"]["items"] + weak["mine"]}
    out = _get_retry(client, reviewed["call"])

    assert out[0]["score"] == 88, "학습 점수가 복습 집계에 덮였다"
    assert out[0]["score"] == by_key["coda_ㄹ"]["score"]
    # 학습 평가 제출 횟수(3)가 이 통화 출현 횟수(7)로 새지 않았다.
    assert out[0]["attempts"] == 7


def test_retry_score_ignores_uncounted_reviews(session_factory, reviewed, monkeypatch):
    """규칙 7 — 스텁 채점(counted=False)이 점수에 섞이지 않는다.

    시드에 coda_ㄹ 100 짜리 counted=False 복습이 있다. 섞이면 (40+50+100)/3 = 63 이
    된다 — 45 가 나와야 한다.
    """
    _patch_report(monkeypatch, [_sound("coda_ㄹ", 7, 3)])
    out = _get_retry(TestClient(_build_app(session_factory)), reviewed["call"])
    assert out[0]["score"] == 45, "counted=False(스텁) 복습이 점수에 섞였다"


def test_retry_score_is_null_when_no_sample(session_factory, lessons, monkeypatch):
    """표본이 없으면 `score=null`(「측정 전」) — 0 으로 뭉개지 않는다.

    `lessons` 픽스처는 복습을 심지 않으므로 집계가 비어 있다.
    """
    _patch_report(monkeypatch, [_sound("coda_ㄹ", 7, 3)])
    out = _get_retry(TestClient(_build_app(session_factory)), lessons["call"])
    assert out[0]["score"] is None, "표본 없는 소리가 0 점으로 나갔다"
    assert "score" in out[0]


# ── 라벨·설명(번역 우선) ────────────────────────────────────────────────── #
def test_retry_label_falls_back_to_korean_without_translation(session_factory, lessons, monkeypatch):
    """번역이 없는 언어(시드 회원 `language="en"`)는 한국어 원본으로 떨어진다."""
    _patch_report(monkeypatch, [_sound("coda_ㄹ", 7, 3)])
    out = _get_retry(TestClient(_build_app(session_factory)), lessons["call"])
    assert out[0]["label"] == "받침 ㄹ"
    assert out[0]["card_desc"] == "받침 ㄹ 소리 내는 법"


def test_retry_label_uses_member_language_translation(session_factory, lessons, monkeypatch):
    """회원 표시 언어(`member.language`) 번역이 있으면 그것을 쓴다.

    ⚠ `member.language` = 모국어다. `target_language`(배우는 언어=한국어)로 바꿔 쓰면
      외국인에게 한국어 설명이 나간다(`weak_sound_service._locale_of` 경고).
    """
    db = session_factory()
    try:
        m = db.get(Member, lessons["member"])
        m.language = "es"
        db.commit()
    finally:
        db.close()

    _patch_report(monkeypatch, [_sound("coda_ㄹ", 7, 3)])
    out = _get_retry(TestClient(_build_app(session_factory)), lessons["call"])
    assert out[0]["label"] == "ㄹ final"
    assert out[0]["card_desc"] == "Pon la punta de la lengua"


# ── 하위호환 ────────────────────────────────────────────────────────────── #
def test_retry_sounds_absent_data_keeps_old_shape(session_factory, seeded, _patch_pron):
    """과·키가 전혀 없는 기존 데이터에서 응답이 **종전과 같다** + `retry_sounds=[]`.

    `_fake_report` 의 sounds 는 전부 `sound_key=None`(옛 집계) 이라 규칙 2 에서 걸린다.
    기존 필드가 하나도 안 바뀌는지 같이 본다(필드 추가가 회귀가 아니어야 한다).
    """
    client = TestClient(_build_app(session_factory))
    r = client.get(f"/api/v1/calls/{seeded['call']}/pronunciation-report", headers=_hdr())
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["retry_sounds"] == []
    # 기존 계약 회귀(test_report_adapts_main_data 와 같은 기대값).
    assert b["total"] == 4 and b["passed"] == 3
    assert [p["sound"] for p in b["phonemes"]] == ["ㄹ", "ㄱ", "ㅗ", "ㅇ"]
    assert b["hardest_sound"] == "ㄹ"
    assert [s["score"] for s in b["sessions"]] == [80, 84, 97]
