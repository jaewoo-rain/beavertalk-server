"""S3(2026-09-26, Play 심사 대비) — 회원 탈퇴 시 alarm·device_token 을 함께 지운다.

배경: `dispatch_service.py` 의 발송 선별이 `member.deleted_at` 을 이제는 보지만(같은
작업), 탈퇴 이전에 이미 만들어진 alarm·device_token 행 자체는 소프트 삭제로 안
지워져 계속 남아 있었다(S2 하드 삭제 전까지 새는 구간). `member_service.delete()`
가 그 두 표를 bulk 로 지워 막는다.

인메모리 SQLite — tests/test_push_dispatch.py 의 PK 치환 패턴을 그대로 쓴다.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base
from domains.account.models.member import Member
from domains.account.service import member_service as msvc
from domains.account.service.member_service import MemberService
from domains.alarm.models.alarm import Alarm
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.push.models.device_token import DeviceToken


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
    sf = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = sf()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _auth_ok(monkeypatch):
    """Supabase auth 삭제는 이 시험의 관심사가 아니다 — 항상 성공으로 둔다."""
    monkeypatch.setattr(msvc, "delete_auth_user", lambda uid: True)


def _seed_member_with_push(db, *, n: int) -> dict:
    voice = Voice(name=f"Fenrir-{n}", gender="male")
    db.add(voice)
    db.flush()
    ch = Character(name="비비", role="선생님", personality="다정", voice_id=voice.voice_id, price=0)
    db.add(ch)
    db.flush()
    member = Member(language="en", korean_level=1, onboarding_completed=True,
                    auth_user_id=f"auth-{n}", email=f"m{n}@x.io")
    db.add(member)
    db.flush()
    db.add(Alarm(member_id=member.member_id, character_id=ch.character_id, is_activate=True))
    db.add(DeviceToken(member_id=member.member_id, platform="android_fcm", token=f"tok-{n}"))
    db.commit()
    return {"member_id": member.member_id}


def _counts(db, member_id: int) -> tuple[int, int]:
    a = db.query(Alarm).filter(Alarm.member_id == member_id).count()
    d = db.query(DeviceToken).filter(DeviceToken.member_id == member_id).count()
    return a, d


def test_delete_purges_alarm_and_device_token(db):
    ids = _seed_member_with_push(db, n=1)
    assert _counts(db, ids["member_id"]) == (1, 1)

    MemberService(db).delete(ids["member_id"])

    assert _counts(db, ids["member_id"]) == (0, 0)
    member = db.get(Member, ids["member_id"])
    assert member.deleted_at is not None
    assert member.email is None


def test_delete_does_not_touch_another_members_push_rows(db):
    """회귀 — 한 회원 탈퇴가 다른(살아있는) 회원의 alarm·device_token 을 건드리면 안 된다."""
    mine = _seed_member_with_push(db, n=1)
    other = _seed_member_with_push(db, n=2)

    MemberService(db).delete(mine["member_id"])

    assert _counts(db, mine["member_id"]) == (0, 0)
    assert _counts(db, other["member_id"]) == (1, 1)


def test_delete_with_no_push_rows_is_a_noop_not_an_error(db):
    """알람·토큰이 원래 없는 회원도 delete() 가 그냥 통과해야 한다."""
    member = Member(language="en", korean_level=1, onboarding_completed=True,
                    auth_user_id="auth-bare", email="bare@x.io")
    db.add(member)
    db.commit()

    MemberService(db).delete(member.member_id)

    assert db.get(Member, member.member_id).deleted_at is not None
    assert _counts(db, member.member_id) == (0, 0)
