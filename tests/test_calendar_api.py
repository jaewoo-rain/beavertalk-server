"""C12(2026-09-23) — 학습 달력 API(CallService.get_calendar).

시험 목록(bt-back 조건⑦ 그대로):
    - 하루(start=end) · 한 달 · 통화 없는 날 제외
    - words 전부 NULL → 키 없음(day·total 둘 다)
    - 시간대 경계(UTC 15:30 = 서울 다음날 00:30)
    - 레벨테스트 제외
    - 범위 초과(400일) 422 · start>end 422 · 날짜 형식 422
    - sentences 는 현지인 짝 포함(소프트 삭제 제외) · call_minutes 는 올림(일별 합이 아니라 전체 초를 한 번만 올림)
근거: docs/plans/2026-09-22-프리미엄-자유대화-15분-달력.md(C12)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.models.sentence import Sentence
from domains.learning.service.call_service import CallService


@pytest.fixture()
def session_factory():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def ctx(session_factory):
    db = session_factory()
    voice = Voice(name="V", gender="male"); db.add(voice); db.flush()
    ch = Character(name="바바", role="선생님", personality="시크", voice_id=voice.voice_id, price=0)
    db.add(ch); db.flush()
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-cal")
    db.add(m); db.flush()
    return {"db": db, "member_id": m.member_id, "cid": ch.character_id}


def _call(ctx, *, when_utc: datetime, total_time=60, status="done", call_type="chat",
          user_word_count: int | None = None, spoke=True):
    c = Call(member_id=ctx["member_id"], character_id=ctx["cid"], call_date=when_utc,
              total_time=total_time, status=status, call_type=call_type,
              user_word_count=user_word_count)
    ctx["db"].add(c); ctx["db"].flush()
    ctx["db"].add(CallRawData(call_id=c.call_id, role="beaver", turn_index=0, content="안녕!"))
    if spoke:
        ctx["db"].add(CallRawData(call_id=c.call_id, role="user", turn_index=1, content="안녕하세요"))
    ctx["db"].commit()
    return c


def _add_sentence(ctx, call: Call, *, native_pair=False, deleted=False):
    base = Sentence(call_id=call.call_id, korean_sentence="감사합니다", locale="en", source_type="asked")
    ctx["db"].add(base); ctx["db"].flush()
    if deleted:
        base.deleted_at = datetime.now(timezone.utc)
    if native_pair:
        pair = Sentence(call_id=call.call_id, korean_sentence="고마워요", locale="en",
                         source_type="asked", kind="native", paired_sentence_id=base.sentence_id)
        ctx["db"].add(pair)
    ctx["db"].commit()


def _cal(ctx, start, end, *, tz=None, tz_offset_min=None):
    return CallService(ctx["db"]).get_calendar(ctx["member_id"], start, end, tz=tz, tz_offset_min=tz_offset_min)


# --------------------------------------------------------------------------- #
# 1) 하루(start=end)
# --------------------------------------------------------------------------- #
def test_single_day_range(ctx):
    call = _call(ctx, when_utc=datetime(2026, 9, 22, 3, 0, tzinfo=timezone.utc),
                 total_time=610, user_word_count=42)  # 610s → ceil(610/60)=11분
    _add_sentence(ctx, call, native_pair=True)  # 기본 1 + 짝 1 = 2문장

    got = _cal(ctx, "2026-09-22", "2026-09-22", tz_offset_min=0)
    assert got["days"] == [
        {"date": "2026-09-22", "sentences": 2, "call_count": 1, "call_minutes": 11, "words": 42},
    ]
    assert got["total"] == {
        "sentences": 2, "call_count": 1, "call_days": 1, "call_minutes": 11, "words": 42,
    }


# --------------------------------------------------------------------------- #
# 2) 한 달 + 통화 없는 날 제외
# --------------------------------------------------------------------------- #
def test_a_month_range_excludes_days_without_calls(ctx):
    _call(ctx, when_utc=datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc), total_time=60, user_word_count=5)
    _call(ctx, when_utc=datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc), total_time=120, user_word_count=10)

    got = _cal(ctx, "2026-09-01", "2026-09-30", tz_offset_min=0)
    dates = [d["date"] for d in got["days"]]
    assert dates == ["2026-09-03", "2026-09-17"]  # 그 사이 27일은 없음
    assert got["total"]["call_days"] == 2
    assert got["total"]["call_count"] == 2


# --------------------------------------------------------------------------- #
# 3) words 전부 NULL → 키 자체가 빠진다(0 금지)
# --------------------------------------------------------------------------- #
def test_words_key_is_omitted_when_never_counted(ctx):
    _call(ctx, when_utc=datetime(2026, 9, 5, 1, 0, tzinfo=timezone.utc), total_time=60, user_word_count=None)

    got = _cal(ctx, "2026-09-01", "2026-09-30", tz_offset_min=0)
    assert "words" not in got["days"][0]
    assert "words" not in got["total"]


# --------------------------------------------------------------------------- #
# 4) 시간대 경계 — UTC 15:30 = 서울(UTC+9) 다음날 00:30
# --------------------------------------------------------------------------- #
def test_timezone_boundary_shifts_the_local_date(ctx):
    _call(ctx, when_utc=datetime(2026, 9, 22, 15, 30, tzinfo=timezone.utc), total_time=60)

    seoul_next_day = _cal(ctx, "2026-09-23", "2026-09-23", tz="Asia/Seoul")
    assert [d["date"] for d in seoul_next_day["days"]] == ["2026-09-23"]

    seoul_same_utc_day = _cal(ctx, "2026-09-22", "2026-09-22", tz="Asia/Seoul")
    assert seoul_same_utc_day["days"] == []  # 서울 기준으로는 22일에 통화가 없다


# --------------------------------------------------------------------------- #
# 5) 레벨테스트 제외
# --------------------------------------------------------------------------- #
def test_level_test_calls_are_excluded(ctx):
    _call(ctx, when_utc=datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc), call_type="level_test")

    got = _cal(ctx, "2026-09-01", "2026-09-30", tz_offset_min=0)
    assert got["days"] == []
    assert got["total"]["call_count"] == 0


# --------------------------------------------------------------------------- #
# 6) 성립 통화만(학습자 발화 0 이면 제외 — has_call_in_window 기준 재사용)
# --------------------------------------------------------------------------- #
def test_calls_where_the_learner_never_spoke_are_excluded(ctx):
    _call(ctx, when_utc=datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc), spoke=False)

    got = _cal(ctx, "2026-09-01", "2026-09-30", tz_offset_min=0)
    assert got["days"] == []


# --------------------------------------------------------------------------- #
# 7) 범위 검증 — 422
# --------------------------------------------------------------------------- #
def test_start_after_end_is_422(ctx):
    with pytest.raises(HTTPException) as exc:
        _cal(ctx, "2026-09-30", "2026-09-01", tz_offset_min=0)
    assert exc.value.status_code == 422


def test_range_over_400_days_is_422(ctx):
    with pytest.raises(HTTPException) as exc:
        _cal(ctx, "2026-01-01", "2027-06-01", tz_offset_min=0)  # > 400일
    assert exc.value.status_code == 422


def test_range_of_exactly_400_days_is_allowed(ctx):
    got = _cal(ctx, "2026-01-01", "2027-02-04", tz_offset_min=0)  # 정확히 400일
    assert got["days"] == []


def test_bad_date_format_is_422(ctx):
    with pytest.raises(HTTPException) as exc:
        _cal(ctx, "2026/09/01", "2026-09-30", tz_offset_min=0)
    assert exc.value.status_code == 422


def test_bad_tz_offset_is_422(ctx):
    with pytest.raises(HTTPException) as exc:
        _cal(ctx, "2026-09-01", "2026-09-30", tz_offset_min=10000)
    assert exc.value.status_code == 422


# --------------------------------------------------------------------------- #
# 8) sentences — 현지인 짝 포함·소프트 삭제 제외
# --------------------------------------------------------------------------- #
def test_sentences_include_native_pairs_and_exclude_soft_deleted(ctx):
    call = _call(ctx, when_utc=datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc))
    _add_sentence(ctx, call, native_pair=True)   # +2 (기본+짝)
    _add_sentence(ctx, call, native_pair=False, deleted=True)  # 삭제 — 안 셈

    got = _cal(ctx, "2026-09-22", "2026-09-22", tz_offset_min=0)
    assert got["days"][0]["sentences"] == 2


# --------------------------------------------------------------------------- #
# 9) call_minutes — 전체 초를 한 번만 올림(일별 합 아님)
# --------------------------------------------------------------------------- #
def test_total_call_minutes_ceils_the_grand_total_not_the_per_day_sum(ctx):
    """3일 각각 25초씩(일별 ceil(25/60)=1분×3=3분) — 그러나 실제 합 75초는 ceil 하면 2분이다."""
    _call(ctx, when_utc=datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc), total_time=25)
    _call(ctx, when_utc=datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc), total_time=25)
    _call(ctx, when_utc=datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc), total_time=25)

    got = _cal(ctx, "2026-09-01", "2026-09-30", tz_offset_min=0)
    assert [d["call_minutes"] for d in got["days"]] == [1, 1, 1]
    assert got["total"]["call_minutes"] == 2  # ceil(75/60) — 일별 합(3)이 아니다
