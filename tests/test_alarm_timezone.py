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
from pydantic import ValidationError
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


# --------------------------------------------------------------------------- #
# §25-①(2026-09-28, 출시 전 권장) — tz_offset_min 범위 검사(-840~840)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [1440, -1440, 841, -841, 10000])
def test_create_rejects_out_of_range_tz_offset_min(bad):
    """⛔⛔ |값|≥1440 이면 dispatch_service._offset_zone 의 datetime.timezone(...)
    이 ValueError 로 죽는다 — API 레벨에서 먼저 422 로 막는다."""
    with pytest.raises(ValidationError):
        AlarmCreate(character_id=1, time=datetime.now(timezone.utc),
                    days_of_week=["MON"], tz_offset_min=bad)


@pytest.mark.parametrize("bad", [1440, -1440, 841])
def test_update_rejects_out_of_range_tz_offset_min(bad):
    with pytest.raises(ValidationError):
        AlarmUpdate(tz_offset_min=bad)


@pytest.mark.parametrize("ok", [-840, 840, 0, 540, -240])
def test_boundary_values_are_accepted(ok):
    """경계값(-840·840, ±14시간)은 통과한다 — 배타적으로 막으면 안 된다."""
    parsed = AlarmCreate(character_id=1, time=datetime.now(timezone.utc),
                         days_of_week=["MON"], tz_offset_min=ok)
    assert parsed.tz_offset_min == ok


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


# --------------------------------------------------------------------------- #
# (2026-09-29) tz 는 유효한 IANA 만 — 아니면 422(오타가 조용히 저장돼 엉뚱한 시각에 울리지 않게)
# --------------------------------------------------------------------------- #
_BAD_TZ = ["Not/AZone", "asia/seoul_x", "KST", "../../etc/passwd", "/abs/path", "x" * 200]


@pytest.mark.parametrize("bad", _BAD_TZ)
def test_create_rejects_invalid_tz(bad):
    with pytest.raises(ValidationError):
        AlarmCreate(character_id=1, time=datetime.now(timezone.utc), days_of_week=["MON"], tz=bad)


@pytest.mark.parametrize("bad", _BAD_TZ)
def test_update_rejects_invalid_tz(bad):
    with pytest.raises(ValidationError):
        AlarmUpdate(tz=bad)


@pytest.mark.parametrize("good", ["Asia/Seoul", "America/New_York", "Pacific/Kiritimati", "UTC", " Europe/Paris "])
def test_valid_iana_tz_passes(good):
    assert AlarmUpdate(tz=good).tz == good.strip()
    assert AlarmCreate(character_id=1, time=datetime.now(timezone.utc),
                       days_of_week=["MON"], tz=good).tz == good.strip()


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_missing_or_empty_tz_is_none_not_422(db, empty):
    """⛔ 미지정·빈 문자열은 «없음» — 422 가 아니고 저장은 NULL(기존 NULL 알람과 같다)."""
    mid = _member(db)
    out = AlarmService(db).create(mid, AlarmCreate(
        character_id=_character_id(db), time=datetime.now(timezone.utc),
        days_of_week=["MON"], tz=empty,
    ))
    assert out.tz is None


def test_update_with_empty_tz_leaves_existing_value(db):
    """update 의 빈 tz 는 «안 보냄»과 같다 — 기존 값을 지우지도, 422 도 아니다."""
    mid = _member(db)
    svc = AlarmService(db)
    created = svc.create(mid, AlarmCreate(
        character_id=_character_id(db), time=datetime.now(timezone.utc),
        days_of_week=["MON"], tz="Asia/Tokyo",
    ))
    assert svc.update(mid, created.alarm_id, AlarmUpdate(tz="")).tz == "Asia/Tokyo"


def test_tz_only_update_is_allowed(db):
    """앱팀 질문 — PUT 에 tz 만 담아도 된다(나머지 필드는 그대로)."""
    mid = _member(db)
    svc = AlarmService(db)
    created = svc.create(mid, AlarmCreate(
        character_id=_character_id(db), time=datetime.now(timezone.utc), days_of_week=["MON", "WED"],
    ))
    updated = svc.update(mid, created.alarm_id, AlarmUpdate(tz="America/New_York"))
    assert updated.tz == "America/New_York"
    assert sorted(updated.days_of_week) == ["MON", "WED"]


def test_schema_validation_agrees_with_dispatch_resolver():
    """⛔ 저장 검증과 디스패치 판정이 **같은 함수**여야 «저장은 됐는데 디스패치가 폴백»이 안 생긴다."""
    from domains.alarm.schemas import alarm as alarm_schemas
    from domains.learning.service import call_service
    from domains.push.service import dispatch_service

    assert alarm_schemas._resolve_zone is call_service._resolve_zone is dispatch_service._resolve_zone
