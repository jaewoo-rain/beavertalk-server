"""숙제 전용 계약·실제 WS 조립·저장 경계. 외부 모델/DB/네트워크 0."""
import copy
import json

import httpx
import pytest

from core import b2b_client
from core.config import settings
from core.homework_contract import namespace_materials, snapshot_hash
from core.prompts.homework import (
    normalize_homework, build_homework_expression_instruction, build_homework_roleplay_instruction,
)
from core.prompts.expression import seed_expression_opening
from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.models.member_item_progress import MemberItemProgress
from domains.learning.realtime import call_session as cs
from domains.learning.service import normalcall_service as svc
from test_normalcall_ws import (
    session_factory, seeded, _mock_external, _auth,
    FakeWebSocket, make_live_factory, _wait_analysis_tasks,
)


def with_mode(raw, mode="expression"):
    """정규화는 대본 방식(mode)을 늘 채운다(PM-DEC-557) — 없으면 표현학습."""
    return {**raw, "mode": mode}


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
    assert data == with_mode(raw)
    text = build_homework_expression_instruction(
        role="역할", personality="성격", level_profile="", locale="vi", name="학습자",
        homework=raw, quiz_group=3)
    assert raw["goals"][0]["surface"].strip() in text
    assert "베트남어" in text
    assert "개인" not in json.dumps(data, ensure_ascii=False)


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
    assert b2b_client.conversation_materials(3, assignment_id=7, locale="vi") == with_mode(raw)
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
    assert raw["goals"][0]["surface"].strip() in holder["system_instruction"]
    # mode 없음 → 표현학습 숙제 — 첫 시드는 정규 표현학습 시드 그대로(PM-DEC-551).
    assert holder["session"].sent_text_turns[0] == seed_expression_opening("한국어")
    started = next(json.loads(s) for s in ws.sent_text if json.loads(s)["type"] == "call_started")
    assert started.get("course") is None
    assert started["fragment_index"] == 1
    assert started["max_fragments"] == 1
    assert started["remaining_s"] > 0
    with session_factory() as db:
        call = db.query(Call).one()
        assert call.call_type == "homework"
        assert namespace_materials(call.usage_json["homework"]) == with_mode(raw)
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
    assert started["remaining_s"] == 300  # 과제당 5분(PM-DEC-551) — 플랜·하루 예산 무관
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


def test_roleplay_reground_uses_same_snapshot_without_personal_sidecar():
    state = cs._CallState()
    raw = materials("manual")
    raw.update(mode="roleplay", roleplay={"situation": "가게에서 인사", "ai_role": "가게 사장님"})
    state.homework = raw
    state.homework_locale = "vi"
    state.homework_mode = "roleplay"
    cs._arm_reground(state, "compression")
    assert state.reground_pending
    assert "가게 사장님" in state.reground_reminder
    assert "안녕하세요" not in state.reground_reminder        # call 2308 — 쪽지에 표현 목록을 다시 싣지 않는다
    assert "베트남어" in state.reground_reminder
    assert state.reground_ctx is None


def test_expression_homework_reground_falls_to_expression_note():
    """표현학습 숙제는 숙제 쪽지 분기를 빠져 정규 표현학습 쪽지(진도 칸)로 간다."""
    state = cs._CallState()
    state.homework = materials("manual")
    state.homework_locale = "vi"
    state.homework_mode = "expression"
    state.expr_items = [{"item_id": 1, "obj": "안녕하세요", "des": None, "ex": None}]
    cs._arm_reground(state, "compression")
    assert state.reground_pending
    assert "[역할극]" not in (state.reground_reminder or "")
    assert state.reground_ctx is None


def test_resume_owner_assignment_and_snapshot(session_factory, seeded, monkeypatch):
    monkeypatch.setattr(b2b_client, "conversation_materials", lambda *a, **k: materials("manual"))
    with session_factory() as db:
        cid = svc.create_call(db, seeded["member_id"], seeded["character_id"], "homework")
        raw = materials("manual")
        svc.save_homework_snapshot(db, cid, raw)
        svc.mark_fragment_ended(db, cid)
        assert svc.load_homework_snapshot(db, seeded["member_id"], cid, 8) == with_mode(raw)
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
            assert namespace_materials(db.get(Call, cid).usage_json["homework"]) == with_mode(raw)


def test_homework_marker_exists_in_first_creation_commit(session_factory, seeded):
    raw = materials()
    with session_factory() as db:
        cid = svc.create_call(db, seeded["member_id"], seeded["character_id"],
                              "homework", homework=raw)
    with session_factory() as fresh:
        assert namespace_materials(fresh.get(Call, cid).usage_json["homework"]) == with_mode(raw)


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
        assert svc.load_homework_snapshot(db, seeded["member_id"], cid, 8) == with_mode(raw)
        assert svc.resume_call(db, seeded["member_id"], cid, max_fragments=3, homework_assignment_id=8)[0] == cid
        saved = db.get(Call, cid).usage_json["homework"]
        assert saved["snapshot"] == initial["snapshot"]
        assert saved["snapshot_hash"] == initial["snapshot_hash"] == snapshot_hash(saved)
        assert saved["runtime"]["generation"] == 2
        assert saved["analysis"]["state"] == "pending"
        prompt = build_homework_expression_instruction(
            role="교사", personality="친절", level_profile="", locale="en", name=None,
            homework=raw, quiz_group=3)
        assert "프롬프트밖원문" not in prompt
        assert "attendance_targets" not in prompt


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


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "roleplay"])
async def test_homework_expression_never_writes_personal_progress(session_factory, seeded, monkeypatch, mode):
    """숙제 목표 id 는 출처별 표(learning_item·assignment_item)라 개인 진도에 쓰면 안 된다 — call_type homework 유지(PM-DEC-557 D2)."""
    raw = materials("manual")
    if mode:
        raw.update(mode=mode, roleplay={"situation": "가게에서 인사", "ai_role": "가게 사장님"})
    monkeypatch.setattr(b2b_client, "conversation_materials", lambda *a, **k: copy.deepcopy(raw))
    writes = []
    monkeypatch.setattr(svc, "save_expression_progress", lambda *a, **k: writes.append("save"))
    monkeypatch.setattr(cs.cur_svc, "record_expression", lambda *a, **k: writes.append("cur"))
    holder = {}
    ws = FakeWebSocket([{"type": "websocket.receive", "text": json.dumps({
        "type": "start", "assignment_id": 8,
    })}])
    await cs.run_call(ws, settings, object(), session_factory,
                      member_id=seeded["member_id"], live_session_factory=make_live_factory(holder))
    await _wait_analysis_tasks()
    assert writes == []
    if mode == "roleplay":
        assert "[역할극]" in holder["system_instruction"]
    else:
        assert holder["session"].sent_text_turns[0] == seed_expression_opening("한국어")
    with session_factory() as db:
        call = db.query(Call).one()
        assert call.call_type == "homework"
        assert call.usage_json["homework"]["snapshot"]["mode"] == (mode or "expression")


@pytest.mark.asyncio
async def test_expression_homework_resume_restores_drill_stage(session_factory, seeded, monkeypatch):
    """표현학습 숙제 재개 — 정규 표현학습처럼 이 통화 전사로 복창 단계를 되살려 쪽지·시드에 싣는다(개인 진도 재료 없음)."""
    from domains.learning.service import call_service
    raw = materials("manual")
    monkeypatch.setattr(b2b_client, "conversation_materials", lambda *a, **k: copy.deepcopy(raw))
    monkeypatch.setattr(call_service, "call_fragments_for_plan", lambda *a: 3)
    first = {}
    ws = FakeWebSocket([{"type": "websocket.receive", "text": json.dumps({
        "type": "start", "assignment_id": 8,
    })}])
    await cs.run_call(ws, settings, object(), session_factory,
                      member_id=seeded["member_id"], live_session_factory=make_live_factory(first))
    await _wait_analysis_tasks()
    with session_factory() as db:
        cid = db.query(Call).one().call_id
    second = {}
    ws = FakeWebSocket([{"type": "websocket.receive", "text": json.dumps({
        "type": "start", "assignment_id": 8, "continues_call_id": str(cid),
    })}])
    await cs.run_call(ws, settings, object(), session_factory,
                      member_id=seeded["member_id"], live_session_factory=make_live_factory(second))
    await _wait_analysis_tasks()
    frames = [json.loads(s) for s in ws.sent_text]
    assert not [f for f in frames if f["type"] == "error"], frames
    started = next(f for f in frames if f["type"] == "call_started")
    assert started["call_id"] == str(cid) and started["fragment_index"] == 2
    # v2 재배포 C2(PM-DEC-698): 이어하기 지시문·시드에 추적기 단계 지시를 싣지 않는다(단계 복원은 관측용으로 그대로)
    assert "[현재 복창 단계]" not in second["system_instruction"]
    assert "[현재 복창 단계]" not in second["session"].sent_text_turns[0]
    assert "[역할극]" not in second["system_instruction"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "roleplay"])
async def test_expression_homework_has_no_hints_roleplay_keeps_them(session_factory, seeded, monkeypatch, mode):
    """PM-DEC-607 — 표현학습 숙제는 정규 표현학습처럼 힌트를 만들지 않는다(정답 노출). 역할극 숙제는 현행 유지(PM-DEC-609).
    앱이 힌트 버튼을 잠그도록 call_started 에 homework_mode 를 싣는다(course 는 그대로 없음)."""
    raw = materials("manual")
    if mode:
        raw.update(mode=mode, roleplay={"situation": "가게에서 인사", "ai_role": "가게 사장님"})
    monkeypatch.setattr(b2b_client, "conversation_materials", lambda *a, **k: copy.deepcopy(raw))
    seen = []
    orig = cs._hint_instruction
    monkeypatch.setattr(cs, "_hint_instruction", lambda *a, **k: (seen.append(True), orig(*a, **k))[1])
    ws = FakeWebSocket([{"type": "websocket.receive", "text": json.dumps({
        "type": "start", "assignment_id": 8,
    })}])
    await cs.run_call(ws, settings, object(), session_factory,
                      member_id=seeded["member_id"], live_session_factory=make_live_factory({}))
    await _wait_analysis_tasks()
    started = next(json.loads(s) for s in ws.sent_text if json.loads(s)["type"] == "call_started")
    assert started["homework_mode"] == (mode or "expression")
    assert started.get("course") is None
    assert bool(seen) is (mode == "roleplay")


def test_call_started_without_homework_mode_keeps_old_bytes():
    """숙제가 아니면 homework_mode 키 자체가 없다 — 구버전 앱·일반 통화 프레임 바이트 동일."""
    from domains.learning.realtime.protocol import ServerCallStarted
    assert "homework_mode" not in ServerCallStarted(character_id=1, call_id="1").model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "roleplay"])
async def test_roleplay_homework_pushes_info_frame_once(session_factory, seeded, monkeypatch, mode):
    """PM-DEC-622·624 — 역할극 숙제만 call_started 뒤 homework_roleplay 1회(원문 + 회원 언어 번역 · 빈 칸 생략).
    표현학습 숙제에는 보내지 않는다."""
    from domains.learning.service import display_i18n_service as i18n_svc
    raw = materials("manual")
    if mode:
        raw.update(mode=mode, roleplay={"situation": "가게에서 인사", "ai_role": "가게 사장님"})
    monkeypatch.setattr(b2b_client, "conversation_materials", lambda *a, **k: copy.deepcopy(raw))
    monkeypatch.setattr(i18n_svc, "homework_roleplay_translations",
                        lambda db, client, rp, locale: {"situation_translation": "Greeting at a shop"})
    ws = FakeWebSocket([{"type": "websocket.receive", "text": json.dumps({
        "type": "start", "assignment_id": 8,
    })}])
    await cs.run_call(ws, settings, object(), session_factory,
                      member_id=seeded["member_id"], live_session_factory=make_live_factory({}))
    await _wait_analysis_tasks()
    frames = [json.loads(s) for s in ws.sent_text]
    infos = [f for f in frames if f["type"] == "homework_roleplay"]
    if mode != "roleplay":
        assert infos == []
        return
    assert infos == [{"type": "homework_roleplay", "situation": "가게에서 인사", "ai_role": "가게 사장님",
                      "situation_translation": "Greeting at a shop"}]
    types = [f["type"] for f in frames]
    assert types.index("call_started") < types.index("homework_roleplay")


@pytest.mark.asyncio
async def test_roleplay_info_frame_survives_translation_failure(session_factory, seeded, monkeypatch):
    """번역이 실패해도 원문 안내는 간다(R5)."""
    from domains.learning.service import display_i18n_service as i18n_svc
    raw = materials("manual")
    raw.update(mode="roleplay", roleplay={"topic": "쇼핑", "ai_role": "점원", "learner_role": "손님"})
    monkeypatch.setattr(b2b_client, "conversation_materials", lambda *a, **k: copy.deepcopy(raw))

    def boom(*a, **k):
        raise RuntimeError("translate down")
    monkeypatch.setattr(i18n_svc, "homework_roleplay_translations", boom)
    ws = FakeWebSocket([{"type": "websocket.receive", "text": json.dumps({
        "type": "start", "assignment_id": 8,
    })}])
    await cs.run_call(ws, settings, object(), session_factory,
                      member_id=seeded["member_id"], live_session_factory=make_live_factory({}))
    await _wait_analysis_tasks()
    infos = [json.loads(s) for s in ws.sent_text if json.loads(s)["type"] == "homework_roleplay"]
    assert infos == [{"type": "homework_roleplay", "topic": "쇼핑", "ai_role": "점원", "learner_role": "손님"}]


def test_homework_roleplay_translations_use_scene_fallback_and_kinds(monkeypatch):
    """상황 카드는 situation 이 없으면 topic 이라 번역도 topic 을 쓴다 · kind 는 hw_situation·hw_role."""
    from domains.learning.service import display_i18n_service as i18n_svc
    seen = []
    monkeypatch.setattr(i18n_svc, "_cur_text_translation",
                        lambda db, client, kind, src, locale: (seen.append((kind, src)), f"{src}-en")[1] if src else None)
    out = i18n_svc.homework_roleplay_translations(None, None, {"topic": "쇼핑", "ai_role": "점원"}, "en")
    assert out == {"situation_translation": "쇼핑-en", "ai_role_translation": "점원-en"}
    assert seen == [("hw_situation", "쇼핑"), ("hw_role", "점원")]  # learner_role 없음 → 부르지 않음
