"""CallService.daily_status — '오늘 통화함' 파생 체크 (외부 의존 0, 인메모리 sqlite).

검증:
    - 로컬 하루 안 **학습자가 말한** done 통화 → called_today True.
    - 학습자 발화 0건 / status ongoing / 어제 통화 → False.
    - 콜타입 분리: 레벨테스트는 called_today 를 켜지 않는다(한도가 따로다).
    - 타임존 경계: 같은 통화라도 클라 로컬 날짜·오프셋에 따라 오늘/어제 갈림.
    - date 형식·tz_offset 범위 오류 → 422.
member 컬럼/일일 초기화 없이 call 테이블에서 EXISTS 파생임을 확인한다.

⚠ 옛 기준(total_time >= 10초)은 폐기됐다. 실측에서 마이크가 안 열린 통화가 10초를
   넘겨 하루를 소모하고 있었다.
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


def _call(ctx, *, when_utc: datetime, total_time=60, status="done",
          spoke=True, call_type="normal"):
    """통화 1건. spoke=True 면 학습자 발화 행을 함께 넣는다(성립 조건).

    선톡은 role='beaver' 라 성립에 안 쓰이므로, 비버 발화만 있는 통화 = spoke False.
    """
    c = Call(member_id=ctx["member_id"], character_id=ctx["cid"],
             call_date=when_utc, total_time=total_time, status=status,
             call_type=call_type)
    ctx["db"].add(c); ctx["db"].flush()
    ctx["db"].add(CallRawData(call_id=c.call_id, role="beaver", turn_index=0,
                              content="안녕! 오늘 뭐 할까?"))  # 선톡 — 성립에 안 쓰임
    if spoke:
        ctx["db"].add(CallRawData(call_id=c.call_id, role="user", turn_index=1,
                                  content="안녕하세요"))
    ctx["db"].commit()
    return c


def _status(ctx, date, tz_offset):
    return CallService(ctx["db"]).daily_status(ctx["member_id"], date, tz_offset)


# --------------------------------------------------------------------------- #
def test_called_today_kst(ctx):
    # KST(+540) 2026-07-17 10:00 = UTC 2026-07-17 01:00 → 로컬 07-17
    _call(ctx, when_utc=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc), total_time=30)
    assert _status(ctx, "2026-07-17", 540) == {
        "date": "2026-07-17", "called_today": True, "level_test_today": False}
    # 같은 통화, 다른 로컬 날짜로 물으면 False
    assert _status(ctx, "2026-07-16", 540)["called_today"] is False


def test_call_without_user_speech_excluded(ctx):
    """★ 학습자가 한마디도 안 한 통화는 하루를 소모하지 않는다.

    실측: normal 405건 중 205건이 발화 0건이고 그중 44건이 10초를 넘겼다(최장 324초).
    마이크가 안 열렸거나 듣기만 한 통화가 한도를 깎으면 안 된다.
    """
    _call(ctx, when_utc=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc),
          total_time=300, spoke=False)
    assert _status(ctx, "2026-07-17", 540)["called_today"] is False


def test_short_call_counts_when_user_spoke(ctx):
    """반대로, 짧아도 학습자가 말했으면 성립한다(옛 10초 기준 폐기)."""
    _call(ctx, when_utc=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc),
          total_time=3, spoke=True)
    assert _status(ctx, "2026-07-17", 540)["called_today"] is True


def test_level_test_does_not_consume_normal(ctx):
    """★ 레벨테스트는 일반 통화 한도를 깎지 않는다(콜타입 분리)."""
    _call(ctx, when_utc=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc),
          call_type="level_test")
    s = _status(ctx, "2026-07-17", 540)
    assert s["called_today"] is False
    assert s["level_test_today"] is True


def test_normal_does_not_consume_level_test(ctx):
    _call(ctx, when_utc=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc),
          call_type="normal")
    s = _status(ctx, "2026-07-17", 540)
    assert s["called_today"] is True
    assert s["level_test_today"] is False


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
