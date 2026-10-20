"""U32-② — 오래 analyzing 에 멈춘 통화를 기동 때 되살린다(off·dry·on · compare-and-set)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.service import analyzing_sweep as sw
from domains.learning.service import normalcall_service as svc


@pytest.fixture()
def sf():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _seed(sf):
    db = sf()
    v = Voice(name="V", gender="male"); db.add(v); db.flush()
    ch = Character(name="바바", role="r", personality="p", voice_id=v.voice_id, price=0); db.add(ch); db.flush()
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-sweep"); db.add(m); db.flush()
    now = datetime.now(timezone.utc)
    ids = {}
    for name, status, ago in (("old", "analyzing", 3600), ("fresh", "analyzing", 60), ("done", "done", 3600)):
        cid = svc.create_call(db, m.member_id, ch.character_id, "chat")
        c = db.get(Call, cid)
        c.status = status
        c.fragment_ended_at = now - timedelta(seconds=ago)
        ids[name] = cid
    db.commit()
    db.close()
    return ids, m.member_id


def test_stale_picks_only_old_analyzing(sf):
    ids, _ = _seed(sf)
    db = sf()
    assert [c for c, _ in sw.stale_analyzing(db, stale_s=1800, limit=20)] == [ids["old"]]
    assert sw.stale_analyzing(db, stale_s=1800, limit=0) == []


def test_claim_is_compare_and_set(sf):
    ids, _ = _seed(sf)
    db = sf()
    assert sw.claim(db, ids["old"]) is True
    assert sw.claim(db, ids["old"]) is False          # 두 번째 인스턴스는 못 가져간다
    assert db.get(Call, ids["old"]).status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "dry"])
async def test_off_and_dry_write_nothing(sf, mode, monkeypatch):
    ids, _ = _seed(sf)
    called = []
    monkeypatch.setattr("domains.learning.realtime.call_session.trigger_reanalysis", lambda *a, **k: called.append(k))
    st = SimpleNamespace(session_factory=sf, genai_client=object())
    out = await sw.sweep(st, SimpleNamespace(ANALYZING_SWEEP=mode, ANALYZING_SWEEP_STALE_S=1800, ANALYZING_SWEEP_LIMIT=20))
    assert out["claimed"] == 0 and not called
    assert out["found"] == (1 if mode == "dry" else 0)
    assert sf().get(Call, ids["old"]).status == "analyzing"


@pytest.mark.asyncio
async def test_on_restarts_reanalysis(sf, monkeypatch):
    ids, member_id = _seed(sf)
    called = []
    monkeypatch.setattr("domains.learning.realtime.call_session.trigger_reanalysis", lambda *a, **k: called.append(k))
    st = SimpleNamespace(session_factory=sf, genai_client=object())
    out = await sw.sweep(st, SimpleNamespace(ANALYZING_SWEEP="on", ANALYZING_SWEEP_STALE_S=1800, ANALYZING_SWEEP_LIMIT=20))
    assert out == {"mode": "on", "found": 1, "claimed": 1, "restarted": 1}
    assert called and called[0]["call_id"] == ids["old"] and called[0]["member_id"] == member_id
    db = sf()
    assert db.get(Call, ids["old"]).status == "analyzing"     # prepare_reanalysis 가 다시 analyzing 으로
    assert db.get(Call, ids["fresh"]).status == "analyzing" and db.get(Call, ids["done"]).status == "done"
