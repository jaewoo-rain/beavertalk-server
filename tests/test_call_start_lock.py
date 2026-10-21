"""통화 시작 잠금(2026-10-22) — 같은 계정 동시 시작 경합(2374·2375)을 잠금 뒤 재확인으로 막는다.

- create_call(exclusive=True): 살아있는 조각이 있으면 행을 만들지 않고 None.
- resume_call: 같은 회원의 다른 통화가 살아있으면 RESUME_REJECT_ALREADY_ACTIVE.
- lock_member_call_start: postgresql 이면 pg_advisory_xact_lock, sqlite 면 아무것도 안 한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import Integer, create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
import domains.learning.service.normalcall_service as svc


@pytest.fixture()
def sf():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            if len(t.primary_key.columns) == 1:
                pk.type = Integer()
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def ids(sf):
    db = sf()
    try:
        v = Voice(name="Fenrir", gender="male"); db.add(v); db.flush()
        ch = Character(name="비비", role="선생님", personality="다정함", voice_id=v.voice_id, price=0); db.add(ch)
        m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-lock-1"); db.add(m)
        db.commit()
        return m.member_id, ch.character_id
    finally:
        db.close()


def _count(sf, member_id):
    db = sf()
    try:
        return db.scalar(select(func.count()).select_from(Call).where(Call.member_id == member_id))
    finally:
        db.close()


def test_exclusive_create_rejects_when_live_fragment(sf, ids):
    member_id, character_id = ids
    db = sf()
    try:
        first = svc.create_call(db, member_id, character_id, "expression", exclusive=True)
        assert first is not None
        # 같은 순간 들어온 두 번째 요청 — 관문은 통과했다고 가정해도 잠금 뒤 재확인에서 막힌다
        assert svc.create_call(db, member_id, character_id, "expression", exclusive=True) is None
    finally:
        db.close()
    assert _count(sf, member_id) == 1


def test_exclusive_create_ignores_ended_and_dead_fragments(sf, ids):
    member_id, character_id = ids
    db = sf()
    try:
        ended = svc.create_call(db, member_id, character_id, "chat")
        dead = svc.create_call(db, member_id, character_id, "chat")
        row = db.get(Call, ended); row.fragment_ended_at = datetime.now(timezone.utc)
        row = db.get(Call, dead); row.fragment_started_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        db.commit()
        assert svc.create_call(db, member_id, character_id, "chat", exclusive=True) is not None
    finally:
        db.close()


def test_default_create_unchanged(sf, ids):
    member_id, character_id = ids
    db = sf()
    try:
        assert svc.create_call(db, member_id, character_id, "chat") is not None
        assert svc.create_call(db, member_id, character_id, "chat") is not None   # 종전 동작(배치·시험 호출부)
    finally:
        db.close()


def test_resume_rejects_when_other_call_live(sf, ids):
    member_id, character_id = ids
    db = sf()
    try:
        prev = svc.create_call(db, member_id, character_id, "expression")
        row = db.get(Call, prev); row.fragment_ended_at = datetime.now(timezone.utc); db.commit()
        svc.create_call(db, member_id, character_id, "expression")   # 다른 통화가 먼저 성립(살아있음)
        call_id, reason = svc.resume_call(db, member_id, prev, max_fragments=3)
        assert call_id is None and reason == svc.RESUME_REJECT_ALREADY_ACTIVE
        assert db.get(Call, prev).fragment_count in (None, 1)
    finally:
        db.close()


def test_resume_ok_when_nothing_live(sf, ids):
    member_id, character_id = ids
    db = sf()
    try:
        prev = svc.create_call(db, member_id, character_id, "expression")
        row = db.get(Call, prev); row.fragment_ended_at = datetime.now(timezone.utc); db.commit()
        call_id, _ = svc.resume_call(db, member_id, prev, max_fragments=3)
        assert call_id == prev
    finally:
        db.close()


def test_lock_sql_only_on_postgres():
    seen = []

    class _Db:
        def __init__(self, name):
            self._name = name

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name=self._name))

        def execute(self, stmt, params=None):
            seen.append((str(stmt), params))

    svc.lock_member_call_start(_Db("sqlite"), 7)
    assert seen == []
    svc.lock_member_call_start(_Db("postgresql"), 7)
    assert seen and "pg_advisory_xact_lock" in seen[0][0]
    assert seen[0][1]["k"] == svc._CALL_START_LOCK_NS + 7
    assert seen[0][1]["k"] < 2 ** 63   # bigint 범위
