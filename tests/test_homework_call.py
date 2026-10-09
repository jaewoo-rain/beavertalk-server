"""숙제 전용 계약·실제 WS 조립·저장 경계. 외부 모델/DB/네트워크 0."""
import copy
import json

import httpx
import pytest

from core import b2b_client
from core.config import settings
from core.homework_contract import namespace_materials, snapshot_hash
from core.prompts.homework import (
    normalize_homework, build_homework_instruction, build_homework_reground,
)
from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.models.member_item_progress import MemberItemProgress
from domains.learning.realtime import call_session as cs
from domains.learning.service import normalcall_service as svc
from test_normalcall_ws import (
    session_factory, seeded, _mock_external, _auth,
    FakeWebSocket, make_live_factory, _wait_analysis_tasks,
)


def materials(source="curriculum", assignment_id=8):
    goal = {"reference": "learning_item", "id": 1, "surface": " 의사{원문} ",
            "meaning": "doctor", "example": None}
    if source == "manual":
        goal.update(reference="assignment_item", surface="안녕하세요",
                    conversation=True, example_meaning=None)
    return {"assignment_id": assignment_id, "source": source, "language": "ko",
            "activities": ["conversation"], "grammar": ["N은/는 N이에요/예요"],
            "goals": [goal]}


@pytest.mark.parametrize("source", ["curriculum", "manual"])
def test_original_source_id_nullable_and_braces_are_preserved(source):
    raw = materials(source)
    data = normalize_homework(raw)
    assert data == raw
    text = build_homework_instruction(role="역할", personality="성격", locale="vi", name="학습자", homework=raw)
    assert raw["goals"][0]["surface"] in text
    assert "베트남어" in text and "1~2문장" in text
    assert "개인" not in json.dumps(data, ensure_ascii=False)
    assert raw["grammar"][0] in build_homework_reground(raw, "vi")


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(activities=["speaking"]),
    lambda d: d.update(goals=[]),
    lambda d: d.update(goals=d["goals"] * 11),
    lambda d: d.update(grammar=None),
    lambda d: d.update(language="en"),
    lambda d: d["goals"][0].update(reference="assignment_item"),
    lambda d: d["goals"][0].update(id=True),
    lambda d: d["goals"][0].update(surface=" "),
])
def test_invalid_materials_are_not_personal_fallback(mutation):
    raw = materials()
    mutation(raw)
    with pytest.raises(ValueError):
        normalize_homework(raw)


def mock_http(monkeypatch, response):
    monkeypatch.setattr(settings, "B2B_API_BASE_URL", "https://b2b.invalid")
    monkeypatch.setattr(settings, "B2B_SERVICE_TOKEN", "fixture-service-token")
    original = httpx.Client
    requests = []
    def handler(req):
        requests.append(req)
        return response(req)
    monkeypatch.setattr(b2b_client.httpx, "Client", lambda **kw: original(
        transport=httpx.MockTransport(handler), **kw,
    ))
    return requests


def test_exact_http_query_and_source_contract(monkeypatch):
    raw = materials("manual", 7)
    requests = mock_http(monkeypatch, lambda req: httpx.Response(200, json=raw))
    assert b2b_client.conversation_materials(3, assignment_id=7, locale="vi") == raw
    req = requests[0]
    assert req.url.path == "/api/v1/internal/members/3/conversation-materials"
    assert dict(req.url.params) == {"assignment_id": "7", "language": "ko", "locale": "vi"}
    assert req.headers["X-Service-Token"] == "fixture-service-token"


@pytest.mark.parametrize("status,detail,code,retry", [
    (404, "assignment_not_found", "assignment_not_found", False),
    (409, "conversation_not_enabled", "conversation_not_enabled", False),
    (409, "conversation_completed", "conversation_completed", False),
    (409, "assignment_closed", "assignment_closed", False),
    (409, "assignment_not_published", "assignment_not_published", False),
    (409, "empty_conversation_materials", "empty_conversation_materials", False),
    (401, "Unauthorized", "homework_service_unavailable", True),
    (404, "Not Found", "homework_service_unavailable", True),
    (422, [{"type": "missing", "loc": ["query", "assignment_id"]}], "homework_service_unavailable", True),
    (503, "unavailable", "homework_service_unavailable", True),
])
def test_http_errors_are_explicit(monkeypatch, status, detail, code, retry):
    mock_http(monkeypatch, lambda req: httpx.Response(status, json={"detail": detail}))
    with pytest.raises(b2b_client.HomeworkMaterialsError) as caught:
        b2b_client.conversation_materials(3, assignment_id=8, locale="en")
    assert (caught.value.code, caught.value.recoverable) == (code, retry)


def test_timeout_and_old_goals_compatibility(monkeypatch):
    def fail(req):
        raise httpx.ReadTimeout("fixture", request=req)
    mock_http(monkeypatch, fail)
    with pytest.raises(b2b_client.HomeworkMaterialsError):
        b2b_client.conversation_materials(3, assignment_id=8, locale="en")
    assert b2b_client.conversation_goal_item_ids(3, assignment_id=8) == []


def result_payload():
    return {"assignment_id": 8, "call_id": 19, "outcome": "linked",
            "performed": True, "met": 1, "total": 2,
            "submission_status": "in_progress"}


def test_result_posts_only_call_identity(monkeypatch):
    payload = result_payload()
    requests = mock_http(monkeypatch, lambda req: httpx.Response(200, json=payload))
    assert b2b_client.conversation_result(3, 8, 19) == payload
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/v1/internal/members/3/assignments/8/conversation-result"
    assert json.loads(requests[0].content) == {"call_id": 19}


@pytest.mark.parametrize("detail,retry", [
    ("homework_not_final", True), ("homework_call_active", True),
    ("homework_analysis_stale", True), ("homework_snapshot_invalid", False),
    ("homework_proof_invalid", False), ("Unauthorized", False),
])
def test_result_error_retry_classification(monkeypatch, detail, retry):
    mock_http(monkeypatch, lambda req: httpx.Response(409, json={"detail": detail}))
    with pytest.raises(b2b_client.HomeworkMaterialsError) as caught:
        b2b_client.conversation_result(3, 8, 19)
    assert (caught.value.code, caught.value.recoverable) == (detail, retry)


@pytest.mark.parametrize("changes", [
    {"assignment_id": 9}, {"call_id": 20}, {"performed": 1},
    {"met": True}, {"total": 0}, {"met": 3},
    {"submission_status": "unknown"}, {"outcome": "unknown"},
])
def test_result_rejects_invalid_ack(monkeypatch, changes):
    payload = result_payload() | changes
    mock_http(monkeypatch, lambda req: httpx.Response(200, json=payload))
    with pytest.raises(b2b_client.HomeworkMaterialsError, match="homework_result_invalid"):
        b2b_client.conversation_result(3, 8, 19)


@pytest.mark.parametrize("elapsed,eligible", [(1, True), (301, False)])
def test_rest_homework_resume_uses_snapshot_eligibility_and_ttl(
    session_factory, seeded, monkeypatch, elapsed, eligible,
):
    from datetime import datetime, timedelta, timezone
    from fastapi.testclient import TestClient
    from test_normalcall_ws import _build_app
    from domains.learning.service import call_service
    from domains.learning.service.homework_service import prepare_analysis
    raw = materials()
    raw["goals"][0]["kind"] = "vocab"
    raw["attendance_targets"] = copy.deepcopy(raw["goals"])
    monkeypatch.setattr(b2b_client, "conversation_materials", lambda *a, **k: copy.deepcopy(raw))
    monkeypatch.setattr(call_service, "call_fragments_for_plan", lambda *a: 3)
    monkeypatch.setattr(call_service, "remaining_budget_s", lambda *a, **k: 600)
    monkeypatch.setattr(svc, "resume_context_is_fresh", lambda *a: True)
    with session_factory() as db:
        cid = svc.create_call(db, seeded["member_id"], seeded["character_id"], "homework", homework=raw)
        svc.mark_fragment_ended(db, cid)
        assert prepare_analysis(db, cid, 1, 1, 3) == (1, 1, -1)
        db.get(Call, cid).fragment_ended_at = datetime.now(timezone.utc) - timedelta(seconds=elapsed)
        db.commit()
    client = TestClient(_build_app(session_factory))
    response = client.get(f"/api/v1/calls/{cid}/resume-status",
                          headers={"Authorization": "Bearer " + seeded["auth"]})
    assert response.status_code == 200
    assert response.json()["can_resume"] is eligible
    assert response.json()["ready"] is eligible


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["curriculum", "manual"])
async def test_ws_homework_bypasses_personal_course_and_persists_raw_source(
    session_factory, seeded, monkeypatch, source,
):
    raw = materials(source)
    monkeypatch.setattr(b2b_client, "conversation_materials", lambda *a, **k: copy.deepcopy(raw))
    def forbidden(*a, **k):
        pytest.fail("숙제에 개인 자료·진도·기억을 주입함")
    monkeypatch.setattr(svc, "load_study_materials", forbidden)
    monkeypatch.setattr(svc, "load_expression_items", forbidden)
    monkeypatch.setattr(cs.cur_svc, "decide_course", forbidden)
    monkeypatch.setattr(cs.chat_memory_service, "load", forbidden)
    holder = {}
    ws = FakeWebSocket([{"type": "websocket.receive", "text": json.dumps({
        "type": "start", "call_type": "auto", "assignment_id": "8",
    })}])
    await cs.run_call(ws, settings, object(), session_factory,
                      member_id=seeded["member_id"], live_session_factory=make_live_factory(holder))
    await _wait_analysis_tasks()
    assert raw["goals"][0]["surface"] in holder["system_instruction"]
    assert "숙제 내용" in holder["session"].sent_text_turns[0]
    started = next(json.loads(s) for s in ws.sent_text if json.loads(s)["type"] == "call_started")
    assert started.get("course") is None
    assert started["fragment_index"] == 1
    assert started["max_fragments"] == 1
    assert started["remaining_s"] > 0
    with session_factory() as db:
        call = db.query(Call).one()
        assert call.call_type == "homework"
        assert namespace_materials(call.usage_json["homework"]) == raw
        assert call.fragment_ended_at is not None
        assert db.query(CallRawData).count() > 0
        assert db.query(MemberItemProgress).count() == 0


@pytest.mark.asyncio
async def test_ws_homework_premium_start_exposes_budget_without_early_delivery(
    session_factory, seeded, monkeypatch,
):
    from datetime import datetime, timedelta, timezone
    from domains.commerce.models.subscribe import Subscribe
    now = datetime.now(timezone.utc)
    with session_factory() as db:
        db.add(Subscribe(member_id=seeded["member_id"], plan="premium", source="manual",
            is_activate=True, is_trial=False, billing_state="ok",
            start_date=now - timedelta(days=1), end_date=now + timedelta(days=1)))
        db.commit()
    raw = materials()
    raw["goals"][0]["kind"] = "vocab"
    raw["attendance_targets"] = copy.deepcopy(raw["goals"])
    monkeypatch.setattr(b2b_client, "conversation_materials", lambda *a, **kw: copy.deepcopy(raw))
    posted = []
    monkeypatch.setattr(b2b_client, "conversation_result", lambda *a, **kw: posted.append(a))
    ws = FakeWebSocket([{"type": "websocket.receive", "text": json.dumps({
        "type": "start", "assignment_id": 8,
    })}])
    await cs.run_call(ws, settings, object(), session_factory,
        member_id=seeded["member_id"], live_session_factory=make_live_factory({}))
    await _wait_analysis_tasks()
    started = next(json.loads(s) for s in ws.sent_text if json.loads(s)["type"] == "call_started")
    assert started["fragment_index"] == 1 and started["max_fragments"] == 3
    assert started["remaining_s"] == 360
    assert not posted
    with session_factory() as db:
        call = db.query(Call).one()
        homework = call.usage_json["homework"]
        assert homework["runtime"]["fragment_count"] == 1
        assert homework["analysis"]["state"] == "ready"
        assert homework["finality"]["state"] == "awaiting_resume"
        assert homework["delivery"]["state"] != "acknowledged"


@pytest.mark.asyncio
async def test_ws_unavailable_materials_never_creates_call(session_factory, seeded, monkeypatch):
    def unavailable(*a, **k):
        raise b2b_client.HomeworkMaterialsError("conversation_not_enabled")
    monkeypatch.setattr(b2b_client, "conversation_materials", unavailable)
    ws = FakeWebSocket([{"type": "websocket.receive", "text": json.dumps({
        "type": "start", "assignment_id": 7,
    })}])
    await cs.run_call(ws, settings, object(), session_factory, member_id=seeded["member_id"])
    assert ws.close_code == 1008
    assert any("HOMEWORK_CONVERSATION_NOT_ENABLED" in s for s in ws.sent_text)
    with session_factory() as db:
        assert db.query(Call).count() == 0


@pytest.mark.asyncio
async def test_ws_empty_verified_result_is_acknowledged_without_personal_progress(
    session_factory, seeded, monkeypatch,
):
    raw = materials()
    raw["goals"][0]["kind"] = "vocab"
    raw["attendance_targets"] = copy.deepcopy(raw["goals"])
    monkeypatch.setattr(b2b_client, "conversation_materials", lambda *a, **k: copy.deepcopy(raw))
    posted = []

    def acknowledge(member_id, assignment_id, call_id):
        posted.append((member_id, assignment_id, call_id))
        return {"assignment_id": assignment_id, "call_id": call_id,
                "outcome": "not_performed", "performed": False,
                "met": 0, "total": 1, "submission_status": "not_started"}

    def forbidden(*a, **k):
        pytest.fail("숙제 결과에 개인 진도 적용을 실행함")

    monkeypatch.setattr(b2b_client, "conversation_result", acknowledge)
    monkeypatch.setattr(svc, "_apply_call_mastery", forbidden)
    ws = FakeWebSocket([{"type": "websocket.receive", "text": json.dumps({
        "type": "start", "assignment_id": 8,
    })}])
    await cs.run_call(ws, settings, object(), session_factory,
                      member_id=seeded["member_id"], live_session_factory=make_live_factory({}))
    await _wait_analysis_tasks()
    with session_factory() as db:
        call = db.query(Call).one()
        namespace = call.usage_json["homework"]
        assert call.status == "done"
        assert namespace["analysis"]["state"] == "ready"
        assert namespace["analysis"]["proof"] == []
        assert namespace["delivery"]["state"] == "acknowledged"
        assert namespace["delivery"]["performed"] is False
        assert posted == [(seeded["member_id"], 8, call.call_id)]
        assert db.query(MemberItemProgress).count() == 0


def test_reground_uses_same_snapshot_without_personal_sidecar():
    state = cs._CallState()
    state.homework = materials("manual")
    state.homework_locale = "vi"
    cs._arm_reground(state, "compression")
    assert state.reground_pending
    assert "assignment_item" in state.reground_reminder
    assert "베트남어" in state.reground_reminder
    assert state.reground_ctx is None


def test_resume_owner_assignment_and_snapshot(session_factory, seeded, monkeypatch):
    monkeypatch.setattr(b2b_client, "conversation_materials", lambda *a, **k: materials("manual"))
    with session_factory() as db:
        cid = svc.create_call(db, seeded["member_id"], seeded["character_id"], "homework")
        raw = materials("manual")
        svc.save_homework_snapshot(db, cid, raw)
        svc.mark_fragment_ended(db, cid)
        assert svc.load_homework_snapshot(db, seeded["member_id"], cid, 8) == raw
        assert svc.resume_call(db, seeded["member_id"], cid, max_fragments=3)[0] is None
        assert svc.resume_call(db, seeded["member_id"], cid, max_fragments=3, homework_assignment_id=9)[0] is None
        assert svc.resume_call(db, seeded["member_id"] + 1, cid, max_fragments=3, homework_assignment_id=8)[0] is None
        assert svc.resume_call(db, seeded["member_id"], cid, max_fragments=3, homework_assignment_id=8)[0] == cid


def test_first_and_accumulated_usage_preserve_homework_snapshot(session_factory, seeded):
    with session_factory() as db:
        cid = svc.create_call(db, seeded["member_id"], seeded["character_id"], "homework")
        raw = materials("manual")
        svc.save_homework_snapshot(db, cid, raw)
        for accumulate in (False, True):
            assert svc.save_call_usage(db, cid, {"msgs": 1, "total": 10}, accumulate=accumulate)
            assert namespace_materials(db.get(Call, cid).usage_json["homework"]) == raw


def test_homework_marker_exists_in_first_creation_commit(session_factory, seeded):
    raw = materials()
    with session_factory() as db:
        cid = svc.create_call(db, seeded["member_id"], seeded["character_id"],
                              "homework", homework=raw)
    with session_factory() as fresh:
        assert namespace_materials(fresh.get(Call, cid).usage_json["homework"]) == raw


def test_full_snapshot_hash_kind_attendance_survive_usage_and_resume(session_factory, seeded, monkeypatch):
    raw = materials()
    raw["goals"][0]["kind"] = "vocab"
    raw["attendance_targets"] = [copy.deepcopy(raw["goals"][0]),
        {"reference": "learning_item", "id": 99, "surface": "프롬프트밖원문",
         "kind": "chunk", "meaning": None, "example": None}]
    monkeypatch.setattr(b2b_client, "conversation_materials", lambda *a, **k: raw)
    with session_factory() as db:
        cid = svc.create_call(db, seeded["member_id"], seeded["character_id"], "homework", homework=raw)
        initial = copy.deepcopy(db.get(Call, cid).usage_json["homework"])
        svc.save_call_usage(db, cid, {"msgs": 1})
        svc.mark_fragment_ended(db, cid)
        assert svc.load_homework_snapshot(db, seeded["member_id"], cid, 8) == raw
        assert svc.resume_call(db, seeded["member_id"], cid, max_fragments=3, homework_assignment_id=8)[0] == cid
        saved = db.get(Call, cid).usage_json["homework"]
        assert saved["snapshot"] == initial["snapshot"]
        assert saved["snapshot_hash"] == initial["snapshot_hash"] == snapshot_hash(saved)
        assert saved["runtime"]["generation"] == 2
        assert saved["analysis"]["state"] == "pending"
        prompt = build_homework_instruction(role="교사", personality="친절", locale="en", name=None, homework=raw)
        assert "프롬프트밖원문" not in prompt
        assert "attendance_targets" not in build_homework_reground(raw, "en")


def test_resume_authorization_timeout_rolls_back_generation(session_factory, seeded, monkeypatch):
    with session_factory() as db:
        cid = svc.create_call(db, seeded["member_id"], seeded["character_id"], "homework", homework=materials())
        svc.mark_fragment_ended(db, cid)
        def timeout(*a, **k):
            raise b2b_client.HomeworkMaterialsError("homework_service_unavailable", recoverable=True)
        monkeypatch.setattr(b2b_client, "conversation_materials", timeout)
        assert svc.resume_call(db, seeded["member_id"], cid, max_fragments=3, homework_assignment_id=8)[0] is None
    with session_factory() as fresh:
        call = fresh.get(Call, cid)
        assert call.fragment_count == 1
        assert call.usage_json["homework"]["runtime"]["generation"] == 1
