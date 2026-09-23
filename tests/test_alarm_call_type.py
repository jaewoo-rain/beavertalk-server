"""알람별 통화 모드(call_type) — 프론트 요청 #1(2026-09-23) 회귀 (외부 의존 0, 인메모리 sqlite).

배경: FCM 페이로드에 알람 id 가 없어 앱은 알람별 모드를 기기에 저장해도 어느 알람의
통화인지 되짚을 수 없다. 서버가 이미 갖고 있는 inbound_call_id → push_dispatch_log →
alarm 경로에 얹는다 — Alarm.call_type("auto"|"chat", 기본 auto).

핵심 불변식:
  - create 가 call_type 없이 오면 "auto", 값이 오면 그 값.
  - update 로 auto↔chat 전환된다(다른 필드와 같은 부분갱신 규율).
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


def test_create_without_call_type_defaults_to_auto(db):
    mid = _member(db)
    out = AlarmService(db).create(mid, AlarmCreate(
        character_id=_character_id(db), time=datetime.now(timezone.utc),
        days_of_week=["MON"],
    ))
    assert out.call_type == "auto"


def test_create_with_chat_is_saved(db):
    mid = _member(db)
    out = AlarmService(db).create(mid, AlarmCreate(
        character_id=_character_id(db), time=datetime.now(timezone.utc),
        days_of_week=["MON"], call_type="chat",
    ))
    assert out.call_type == "chat"


def test_update_switches_auto_to_chat(db):
    mid = _member(db)
    svc = AlarmService(db)
    created = svc.create(mid, AlarmCreate(
        character_id=_character_id(db), time=datetime.now(timezone.utc),
        days_of_week=["MON"],
    ))
    assert created.call_type == "auto"
    updated = svc.update(mid, created.alarm_id, AlarmUpdate(call_type="chat"))
    assert updated.call_type == "chat"


def test_update_switches_chat_back_to_auto(db):
    mid = _member(db)
    svc = AlarmService(db)
    created = svc.create(mid, AlarmCreate(
        character_id=_character_id(db), time=datetime.now(timezone.utc),
        days_of_week=["MON"], call_type="chat",
    ))
    updated = svc.update(mid, created.alarm_id, AlarmUpdate(call_type="auto"))
    assert updated.call_type == "auto"


def test_update_without_call_type_leaves_it_unchanged(db):
    """다른 필드와 같은 부분갱신 규율 — call_type 을 안 보내면 안 건드린다."""
    mid = _member(db)
    svc = AlarmService(db)
    created = svc.create(mid, AlarmCreate(
        character_id=_character_id(db), time=datetime.now(timezone.utc),
        days_of_week=["MON"], call_type="chat",
    ))
    updated = svc.update(mid, created.alarm_id, AlarmUpdate(is_activate=False))
    assert updated.call_type == "chat"


def test_invalid_call_type_is_rejected_by_the_schema():
    """⚠ Literal 로 받는다 — 422 는 서버 몫이지 검사 코드를 새로 짜지 않는다."""
    with pytest.raises(Exception):
        AlarmCreate(character_id=1, time=datetime.now(timezone.utc),
                    days_of_week=["MON"], call_type="normal")
