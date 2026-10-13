"""PM-DEC-551·552·556(2026-10-13) — 숙제 통화는 하루 예산에서 빠지고 과제당 누적 5분을 따로 센다."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from core import b2b_client
from core.config import settings
from domains.learning.models.call import Call
from domains.learning.realtime import call_session as cs
from domains.learning.service import call_service
from domains.learning.service import normalcall_service as svc
from domains.learning.service.homework_service import prepare_analysis
from test_homework_call import materials
from test_normalcall_ws import (  # noqa: F401  (픽스처)
    session_factory, seeded, _auth, FakeWebSocket, make_live_factory, _wait_analysis_tasks,
)


@pytest.fixture(autouse=True)
def _budget_enforced(monkeypatch):
    monkeypatch.setattr(call_service.settings, "DAILY_BUDGET_ENFORCED", True, raising=False)


def _raw(assignment_id=8):
    raw = materials(assignment_id=assignment_id)
    raw["goals"][0]["kind"] = "vocab"
    raw["attendance_targets"] = copy.deepcopy(raw["goals"])
    return raw


def _homework_call(db, seeded, seconds, assignment_id=8):
    cid = svc.create_call(db, seeded["member_id"], seeded["character_id"], "homework",
                          homework=_raw(assignment_id))
    svc.mark_fragment_ended(db, cid, total_time=seconds)
    return cid


def _chat_call(db, seeded, seconds):
    now = datetime.now(timezone.utc)
    db.add(Call(member_id=seeded["member_id"], character_id=seeded["character_id"], call_date=now,
                total_time=seconds, status="done", call_type="chat",
                fragment_started_at=now - timedelta(seconds=seconds), fragment_ended_at=now))
    db.commit()


async def _start_homework(session_factory, seeded, monkeypatch, assignment_id=8):
    monkeypatch.setattr(b2b_client, "conversation_materials",
                        lambda *a, **k: copy.deepcopy(_raw(assignment_id)))
    ws = FakeWebSocket([{"type": "websocket.receive", "text": json.dumps({
        "type": "start", "assignment_id": assignment_id,
    })}])
    await cs.run_call(ws, settings, object(), session_factory,
                      member_id=seeded["member_id"], live_session_factory=make_live_factory({}))
    await _wait_analysis_tasks()
    return [json.loads(s) for s in ws.sent_text]


def test_homework_does_not_consume_daily_budget(session_factory, seeded):
    with session_factory() as db:
        _homework_call(db, seeded, 300)
        assert call_service.used_seconds_today(db, seeded["member_id"]) == 0
        assert call_service.daily_budget_exceeded(db, seeded["member_id"]) is False


def test_homework_time_is_summed_per_assignment_across_calls(session_factory, seeded):
    with session_factory() as db:
        _homework_call(db, seeded, 100)
        _homework_call(db, seeded, 80)
        _homework_call(db, seeded, 250, assignment_id=9)
        assert call_service.homework_used_s(db, seeded["member_id"], 8) == 180
        assert call_service.homework_remaining_s(db, seeded["member_id"], 8) == 120
        assert call_service.homework_used_s(db, seeded["member_id"], 9) == 250
        assert call_service.homework_remaining_s(db, seeded["member_id"], 10) == 300


def test_few_leftover_seconds_count_as_exhausted(session_factory, seeded):
    with session_factory() as db:
        _homework_call(db, seeded, 300 - call_service.HOMEWORK_MIN_START_S + 1)
        assert call_service.homework_remaining_s(db, seeded["member_id"], 8) == 0


@pytest.mark.asyncio
async def test_ws_homework_ignores_spent_daily_budget(session_factory, seeded, monkeypatch):
    with session_factory() as db:
        _chat_call(db, seeded, 300)
        assert call_service.daily_budget_exceeded(db, seeded["member_id"]) is True
    frames = await _start_homework(session_factory, seeded, monkeypatch)
    assert not any(f.get("code") == "DAILY_LIMIT" for f in frames)
    started = next(f for f in frames if f["type"] == "call_started")
    assert started["remaining_s"] == 300


@pytest.mark.asyncio
async def test_ws_homework_remaining_continues_from_earlier_calls(session_factory, seeded, monkeypatch):
    with session_factory() as db:
        _homework_call(db, seeded, 120)
    frames = await _start_homework(session_factory, seeded, monkeypatch)
    started = next(f for f in frames if f["type"] == "call_started")
    assert started["remaining_s"] == 180


@pytest.mark.asyncio
async def test_ws_homework_rejected_after_five_minutes(session_factory, seeded, monkeypatch):
    with session_factory() as db:
        _homework_call(db, seeded, 200)
        _homework_call(db, seeded, 100)
        before = db.query(Call).count()
    frames = await _start_homework(session_factory, seeded, monkeypatch)
    errors = [f for f in frames if f["type"] == "error"]
    assert [e["code"] for e in errors] == ["HOMEWORK_TIME_LIMIT"]
    assert errors[0]["recoverable"] is False
    assert not any(f["type"] == "call_started" for f in frames)
    with session_factory() as db:
        assert db.query(Call).count() == before


@pytest.mark.asyncio
async def test_ws_other_assignment_still_has_its_own_five_minutes(session_factory, seeded, monkeypatch):
    with session_factory() as db:
        _homework_call(db, seeded, 300)
    frames = await _start_homework(session_factory, seeded, monkeypatch, assignment_id=9)
    started = next(f for f in frames if f["type"] == "call_started")
    assert started["remaining_s"] == 300


def test_analysis_marks_final_when_assignment_time_is_spent(session_factory, seeded):
    with session_factory() as db:
        _homework_call(db, seeded, 180)
        cid = _homework_call(db, seeded, 120)
        assert prepare_analysis(db, cid, 1, 1, 3) == (1, 1, -1)
        homework = db.get(Call, cid).usage_json["homework"]
        assert homework["time"] == {"used_s": 300, "limit_s": 300, "exhausted": True}
        assert homework["finality"] == {"state": "final", "reason": "budget_exhausted",
                                        "max_fragments": 1, "time_exhausted": True}


def test_analysis_waits_for_resume_while_time_remains(session_factory, seeded):
    with session_factory() as db:
        cid = _homework_call(db, seeded, 100)
        assert prepare_analysis(db, cid, 1, 1, 3) == (1, 1, -1)
        homework = db.get(Call, cid).usage_json["homework"]
        assert homework["time"] == {"used_s": 100, "limit_s": 300, "exhausted": False}
        assert homework["finality"] == {"state": "awaiting_resume"}


def test_rest_resume_status_uses_assignment_time(session_factory, seeded, monkeypatch):
    from fastapi.testclient import TestClient
    from test_normalcall_ws import _build_app
    monkeypatch.setattr(b2b_client, "conversation_materials", lambda *a, **k: copy.deepcopy(_raw()))
    monkeypatch.setattr(call_service, "call_fragments_for_plan", lambda *a: 3)
    monkeypatch.setattr(call_service, "remaining_budget_s", lambda *a, **k: 0)
    monkeypatch.setattr(svc, "resume_context_is_fresh", lambda *a: True)
    with session_factory() as db:
        cid = _homework_call(db, seeded, 100)
        assert prepare_analysis(db, cid, 1, 1, 3) == (1, 1, -1)
    client = TestClient(_build_app(session_factory))
    headers = {"Authorization": "Bearer " + seeded["auth"]}
    first = client.get(f"/api/v1/calls/{cid}/resume-status", headers=headers)
    assert first.json()["can_resume"] is True
    with session_factory() as db:
        _homework_call(db, seeded, 200)
    assert client.get(f"/api/v1/calls/{cid}/resume-status", headers=headers).json()["can_resume"] is False
