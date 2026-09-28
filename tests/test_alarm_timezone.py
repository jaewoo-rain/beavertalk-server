"""알람 시간대(tz·tz_offset_min) — §5(2026-09-28) 회귀 (외부 의존 0, 인메모리 sqlite).

핵심 불변식(docs/plans/2026-09-28-알람시간대-해지사유-5-17.md §5):
  - 둘 다 생략하면 NULL 로 저장된다 — 서버가 서울을 기본값으로 채우지 않는다
    ("앱이 안 보냈다"와 "진짜 서울이다"가 구분돼야 한다, 폴백은 dispatch_service
    한 곳에만).
  - AlarmOut 은 저장값을 그대로 낸다 — 없는 값을 지어내지 않는다.
  - update 로 두 필드를 나중에 채울 수 있다(다른 필드와 같은 부분갱신 규율).

디스패치가 이 값을 실제로 어떻게 쓰는지(존 우선순위·DST·멱등 키)는
tests/test_push_dispatch.py 섹션 F 가 검증한다 — 이 파일은 저장·응답 계약만 본다.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401 - 전 모델 import
from domains.account.models.member import Member
from domains.alarm.schemas.alarm import AlarmCreate, AlarmUpdate
from domains.alarm.service.alarm_service import AlarmService
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice


@pytest.fixture()
def db():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    v = Voice(name="Leda", gender="female")
    s.add(v)
    s.flush()
    s.add(Character(name="BABA", role="r", personality="p", voice_id=v.voice_id, price=0))
    s.commit()
    return s


def _member(db) -> int:
    m = Member(language="en", onboarding_completed=True, auth_user_id="a1")
    db.add(m)
    db.commit()
    return m.member_id


def _character_id(db) -> int:
    return db.query(Character).filter_by(name="BABA").one().character_id


def test_create_without_tz_stores_null_not_seoul(db):
    """⛔ 서버가 서울을 기본값으로 채우면 안 된다 — "안 보냄"이 그대로 NULL 이어야 한다."""
    mid = _member(db)
    out = AlarmService(db).create(mid, AlarmCreate(
        character_id=_character_id(db), time=datetime.now(timezone.utc),
        days_of_week=["MON"],
    ))
    assert out.tz is None
    assert out.tz_offset_min is None


def test_create_with_tz_is_saved_and_echoed(db):
    mid = _member(db)
    out = AlarmService(db).create(mid, AlarmCreate(
        character_id=_character_id(db), time=datetime.now(timezone.utc),
        days_of_week=["MON"], tz="America/New_York", tz_offset_min=-240,
    ))
    assert out.tz == "America/New_York"
    assert out.tz_offset_min == -240


def test_update_sets_tz_on_an_alarm_created_without_one(db):
    """앱이 알람을 수정할 때 새 tz 로 덮인다(요청서 규칙) — 기존 17행이 이 경로로 채워진다."""
    mid = _member(db)
    svc = AlarmService(db)
    created = svc.create(mid, AlarmCreate(
        character_id=_character_id(db), time=datetime.now(timezone.utc),
        days_of_week=["MON"],
    ))
    assert created.tz is None
    updated = svc.update(mid, created.alarm_id, AlarmUpdate(tz="Asia/Tokyo", tz_offset_min=540))
    assert updated.tz == "Asia/Tokyo"
    assert updated.tz_offset_min == 540


def test_update_without_tz_fields_leaves_existing_tz_untouched(db):
    """부분갱신 — tz 를 안 보내면 기존 값을 건드리지 않는다(다른 필드와 같은 규율)."""
    mid = _member(db)
    svc = AlarmService(db)
    created = svc.create(mid, AlarmCreate(
        character_id=_character_id(db), time=datetime.now(timezone.utc),
        days_of_week=["MON"], tz="America/New_York", tz_offset_min=-240,
    ))
    updated = svc.update(mid, created.alarm_id, AlarmUpdate(call_type="chat"))
    assert updated.tz == "America/New_York"
    assert updated.tz_offset_min == -240


def test_legacy_client_payload_without_tz_fields_is_ignored_not_422(db):
    """구서버 호환 확인(plan §5-5) — pydantic v2 기본 extra=ignore 라 tz 를 안 보내는
    구버전 앱 페이로드도 422 없이 그대로 통과한다(신규 필드는 전부 Optional)."""
    payload = {
        "character_id": _character_id(db), "time": datetime.now(timezone.utc).isoformat(),
        "days_of_week": ["MON"],
    }
    parsed = AlarmCreate.model_validate(payload)
    assert parsed.tz is None
    assert parsed.tz_offset_min is None
