"""CallService.daily_status — '오늘 통화함' 파생 체크 (외부 의존 0, 인메모리 sqlite).

검증:
    - 로컬 하루 안 10초+ done 통화 → called_today True.
    - total_time<10 / status ongoing / 어제 통화 → False.
    - 타임존 경계: 같은 통화라도 클라 로컬 날짜·오프셋에 따라 오늘/어제 갈림.
    - date 형식·tz_offset 범위 오류 → 422.
member 컬럼/일일 초기화 없이 call 테이블에서 EXISTS 파생임을 확인한다.
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
from domains.learning.service.call_service import CallService


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
def ctx(session_factory):
    db = session_factory()
    voice = Voice(name="V", gender="male"); db.add(voice); db.flush()
    ch = Character(name="바바", role="선생님", personality="시크", voice_id=voice.voice_id, price=0)
    db.add(ch); db.flush()
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-d")
    db.add(m); db.flush()
    return {"db": db, "member_id": m.member_id, "cid": ch.character_id}


def _call(ctx, *, when_utc: datetime, total_time, status="done"):
    c = Call(member_id=ctx["member_id"], character_id=ctx["cid"],
             call_date=when_utc, total_time=total_time, status=status)
    ctx["db"].add(c); ctx["db"].commit()
    return c


def _status(ctx, date, tz_offset):
    return CallService(ctx["db"]).daily_status(ctx["member_id"], date, tz_offset)


# --------------------------------------------------------------------------- #
def test_called_today_kst(ctx):
    # KST(+540) 2026-07-17 10:00 = UTC 2026-07-17 01:00 → 로컬 07-17
    _call(ctx, when_utc=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc), total_time=30)
    assert _status(ctx, "2026-07-17", 540) == {"date": "2026-07-17", "called_today": True}
    # 같은 통화, 다른 로컬 날짜로 물으면 False
    assert _status(ctx, "2026-07-16", 540)["called_today"] is False


def test_short_call_excluded(ctx):
    _call(ctx, when_utc=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc), total_time=9)
    assert _status(ctx, "2026-07-17", 540)["called_today"] is False


def test_ongoing_excluded(ctx):
    _call(ctx, when_utc=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc), total_time=None, status="ongoing")
    assert _status(ctx, "2026-07-17", 540)["called_today"] is False


def test_analyzing_counts(ctx):
    _call(ctx, when_utc=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc), total_time=20, status="analyzing")
    assert _status(ctx, "2026-07-17", 540)["called_today"] is True


def test_yesterday_excluded(ctx):
    # UTC 07-16 03:00 = KST 07-16 12:00 → 로컬 07-16(어제)
    _call(ctx, when_utc=datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc), total_time=30)
    assert _status(ctx, "2026-07-17", 540)["called_today"] is False


def test_tz_boundary_split(ctx):
    # UTC 07-16 20:00 → KST(+540) 07-17 05:00(오늘) / UTC(0) 07-16(어제)
    _call(ctx, when_utc=datetime(2026, 7, 16, 20, 0, tzinfo=timezone.utc), total_time=30)
    assert _status(ctx, "2026-07-17", 540)["called_today"] is True    # KST 기준 오늘
    assert _status(ctx, "2026-07-17", 0)["called_today"] is False      # UTC 기준 어제


def test_no_call_false(ctx):
    assert _status(ctx, "2026-07-17", 540)["called_today"] is False


def test_bad_date_422(ctx):
    with pytest.raises(HTTPException) as e:
        _status(ctx, "2026/07/17", 540)
    assert e.value.status_code == 422


def test_bad_offset_422(ctx):
    with pytest.raises(HTTPException) as e:
        _status(ctx, "2026-07-17", 9999)
    assert e.value.status_code == 422
