"""C13(2026-09-23) — 연속일(streak_days, CallService._streak_days / get_calendar 통합).

시험 목록(bt-back 조건⑤ 그대로):
    - 오늘 포함 연속
    - 오늘 없음 + 어제까지 연속
    - 중간 끊김
    - 0 이면 키 생략
    - 999 상한
    - 시간대 경계(UTC 15:30 통화가 서울 기준 다음날)

⚠ "오늘"은 실제 wall-clock now 를 쓴다(local_window_utc·_streak_days 가 그렇게
  설계됨 — 조건② «기간과 무관, 지금 기준») — 다른 파일(test_daily_call_budget.py)
  도 같은 방식으로 시험한다.
근거: docs/plans/2026-09-22-프리미엄-자유대화-15분-달력.md(C13)
"""

from __future__ import annotations

from datetime import datetime, time as _time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
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
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-streak")
    db.add(m); db.flush()
    return {"db": db, "member_id": m.member_id, "cid": ch.character_id}


def _call_on(ctx, when_utc: datetime, *, spoke=True):
    c = Call(member_id=ctx["member_id"], character_id=ctx["cid"], call_date=when_utc,
              total_time=60, status="done", call_type="chat")
    ctx["db"].add(c); ctx["db"].flush()
    ctx["db"].add(CallRawData(call_id=c.call_id, role="beaver", turn_index=0, content="안녕!"))
    if spoke:
        ctx["db"].add(CallRawData(call_id=c.call_id, role="user", turn_index=1, content="안녕하세요"))
    ctx["db"].commit()
    return c


def _now_utc_at_hour(hour: int = 3) -> datetime:
    """오늘(UTC) 특정 시각 — 자정 근처 경합을 피해 하루 중간(03:00 UTC)에 찍는다."""
    return datetime.now(timezone.utc).replace(hour=hour, minute=0, second=0, microsecond=0)


def _calendar(ctx, *, tz=None, tz_offset_min=None):
    # 조건②: 기간과 무관 — 아무 좁은 과거 범위를 물어봐도 streak_days 는 "지금" 기준.
    return CallService(ctx["db"]).get_calendar(ctx["member_id"], "2020-01-01", "2020-01-01",
                                                tz=tz, tz_offset_min=tz_offset_min)


# --------------------------------------------------------------------------- #
# 1) 오늘 포함 연속
# --------------------------------------------------------------------------- #
def test_streak_counts_consecutive_days_including_today(ctx):
    today = _now_utc_at_hour()
    for n in range(3):  # 오늘, 어제, 그제 — 3일 연속
        _call_on(ctx, today - timedelta(days=n))
    got = _calendar(ctx, tz_offset_min=0)
    assert got["streak_days"] == 3


# --------------------------------------------------------------------------- #
# 2) 오늘 없음 + 어제까지 연속
# --------------------------------------------------------------------------- #
def test_streak_falls_back_to_yesterday_when_today_has_no_call(ctx):
    today = _now_utc_at_hour()
    for n in range(1, 4):  # 어제·그제·그끄제 — 오늘은 없음
        _call_on(ctx, today - timedelta(days=n))
    got = _calendar(ctx, tz_offset_min=0)
    assert got["streak_days"] == 3


# --------------------------------------------------------------------------- #
# 3) 중간 끊김
# --------------------------------------------------------------------------- #
def test_streak_stops_at_a_gap(ctx):
    today = _now_utc_at_hour()
    _call_on(ctx, today)               # 오늘
    _call_on(ctx, today - timedelta(days=1))  # 어제
    # 그제(2일 전)는 건너뛴다 — 끊김
    _call_on(ctx, today - timedelta(days=3))
    got = _calendar(ctx, tz_offset_min=0)
    assert got["streak_days"] == 2


# --------------------------------------------------------------------------- #
# 4) 0 이면 키 생략(오늘·어제 둘 다 없음)
# --------------------------------------------------------------------------- #
def test_streak_key_is_omitted_when_zero(ctx):
    today = _now_utc_at_hour()
    _call_on(ctx, today - timedelta(days=5))  # 오늘·어제와 이어지지 않음
    got = _calendar(ctx, tz_offset_min=0)
    assert "streak_days" not in got


def test_streak_key_is_omitted_with_no_calls_at_all(ctx):
    got = _calendar(ctx, tz_offset_min=0)
    assert "streak_days" not in got


# --------------------------------------------------------------------------- #
# 5) 999 상한
# --------------------------------------------------------------------------- #
def test_streak_caps_at_999(ctx):
    today = _now_utc_at_hour()
    for n in range(1005):  # 999 보다 훨씬 긴 연속 기록
        _call_on(ctx, today - timedelta(days=n))
    got = _calendar(ctx, tz_offset_min=0)
    assert got["streak_days"] == 999


# --------------------------------------------------------------------------- #
# 6) 시간대 경계 — 같은 두 통화가 tz 에 따라 "이틀"로도 "하루"로도 묶인다
# --------------------------------------------------------------------------- #
def test_streak_timezone_boundary(ctx):
    """⭐⭐ 실시각(now)에 무관하게 결정적이어야 하므로, "오늘"을 가정하지 않고
    **서울 기준 오늘 날짜**(now 그대로, 언제 돌든 상관없음)를 직접 구해 그 위에
    두 통화를 심는다.

    P = 서울 오늘 02:00, Q = 서울 어제 20:00 — 서울 기준으로는 연속 이틀(오늘·어제).
    두 시각을 UTC 로 그대로 환산하면(-9h) 각각 (오늘-1일) 17:00 · (오늘-1일) 11:00 —
    **같은 UTC 날짜 하루**로 뭉친다. 그래서 UTC(tz_offset_min=0)로 보면 streak=1,
    서울(tz=Asia/Seoul)로 보면 streak=2 — 같은 데이터, 다른 tz, 다른 답.
    """
    seoul = ZoneInfo("Asia/Seoul")
    today_seoul = datetime.now(seoul).date()
    p_seoul = datetime.combine(today_seoul, _time(2, 0), tzinfo=seoul)
    q_seoul = datetime.combine(today_seoul - timedelta(days=1), _time(20, 0), tzinfo=seoul)

    _call_on(ctx, p_seoul.astimezone(timezone.utc))
    _call_on(ctx, q_seoul.astimezone(timezone.utc))

    got_seoul = _calendar(ctx, tz="Asia/Seoul")
    assert got_seoul["streak_days"] == 2

    got_utc = _calendar(ctx, tz_offset_min=0)
    assert got_utc["streak_days"] == 1


# --------------------------------------------------------------------------- #
# 7) 조건③ — 캐시된 범위가 스트릭 창을 덮으면 재쿼리하지 않고 재사용한다
# --------------------------------------------------------------------------- #
def test_streak_reuses_cached_rows_when_the_cached_range_covers_the_window(ctx):
    """⛔ get_calendar 는 요청 범위를 최대 400일로 막아 스트릭 창(999일)을 실제로
    한 번에 덮을 수 없다 — 그래서 `_streak_days` 를 직접 불러 재사용 경로(넓은
    cached_range)를 시험한다. repo.calendar_calls 가 **다시 불리지 않는지** 감시."""
    today = _now_utc_at_hour()
    _call_on(ctx, today)
    _call_on(ctx, today - timedelta(days=1))

    svc = CallService(ctx["db"])
    huge_range = (datetime(2000, 1, 1, tzinfo=timezone.utc), datetime(2100, 1, 1, tzinfo=timezone.utc))
    cached_rows = svc.repo.calendar_calls(ctx["member_id"], *huge_range)

    call_count = {"n": 0}
    orig = svc.repo.calendar_calls

    def _counting(*a, **kw):
        call_count["n"] += 1
        return orig(*a, **kw)

    svc.repo.calendar_calls = _counting
    streak = svc._streak_days(
        ctx["member_id"], tz=None, tz_offset_min=0, zone=None, offset=timedelta(minutes=0),
        cached_rows=cached_rows, cached_range=huge_range,
    )
    assert streak == 2
    assert call_count["n"] == 0  # 재쿼리 없이 cached_rows 그대로 썼다
