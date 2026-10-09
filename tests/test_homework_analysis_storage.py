from datetime import datetime, timezone

from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.service import normalcall_service as svc
from domains.learning.service.homework_service import commit_verified_analysis
from domains.learning.service.homework_service import deliver_homework
from domains.learning.service.homework_service import recover_pending, set_analysis_state
from core import b2b_client
from datetime import timedelta
from test_normalcall_ws import session_factory, seeded, _mock_external


def setup_call(db, seeded):
    cid = svc.create_call(db, seeded["member_id"], seeded["character_id"], "homework")
    call = db.get(Call, cid)
    call.fragment_count = 1
    call.fragment_ended_at = datetime.now(timezone.utc)
    call.usage_json = {"homework": {
        "source": "curriculum", "snapshot": {"attendance_targets": [
            {"reference": "learning_item", "id": 99, "kind": "chunk", "surface": "안녕하세요"}]},
        "runtime": {"generation": 1, "fragment_count": 1, "active": False,
                    "through_turn_index": 0},
        "analysis": {"state": "pending"}, "delivery": {"state": "pending"}}}
    db.add(CallRawData(call_id=cid, role="user", turn_index=0, content="안녕하세요"))
    db.commit()
    return cid


def test_ready_and_proof_are_visible_in_new_session_after_transcript_commit(session_factory, seeded):
    with session_factory() as db:
        cid = setup_call(db, seeded)
        assert commit_verified_analysis(db, cid, (1, 1, 0), [
            svc.ItemDetection(item_id=1, evidence="E2", quote="안녕하세요")])
    with session_factory() as fresh:
        data = fresh.get(Call, cid).usage_json["homework"]
        assert data["analysis"]["state"] == "ready"
        assert data["analysis"]["proof"][0]["id"] == 99
        assert data["delivery"]["state"] == "pending"


def test_late_analysis_cannot_overwrite_resumed_pending(session_factory, seeded):
    with session_factory() as db:
        cid = setup_call(db, seeded)
        call = db.get(Call, cid)
        namespace = dict(call.usage_json["homework"])
        namespace["runtime"] = {**namespace["runtime"], "generation": 2, "active": True,
                                "fragment_count": 2}
        call.fragment_count = 2
        call.fragment_ended_at = None
        call.usage_json = {"homework": namespace}
        db.commit()
        assert not commit_verified_analysis(db, cid, (1, 1, 0), [])
        assert not svc._save_call_title(db, cid, "늦은 제목", "en", homework_expected=(1, 1, 0))
        svc._save_resume_context(db, cid, {"topic": "늦은 요약"}, homework_expected=(1, 1, 0))
        assert db.get(Call, cid).summary is None
        assert db.get(Call, cid).resume_context is None
    with session_factory() as fresh:
        assert fresh.get(Call, cid).usage_json["homework"]["analysis"]["state"] == "pending"


def test_late_committed_transcript_invalidates_old_range(session_factory, seeded):
    with session_factory() as db:
        cid = setup_call(db, seeded)
        db.add(CallRawData(call_id=cid, role="user", turn_index=1, content="추가 발화"))
        db.commit()
        assert not commit_verified_analysis(db, cid, (1, 1, 0), [])


def test_lost_ack_recovers_same_call_with_backoff_and_idempotent_result(session_factory, seeded, monkeypatch):
    with session_factory() as db:
        cid = setup_call(db, seeded)
        call = db.get(Call, cid)
        data = dict(call.usage_json["homework"])
        data["assignment_id"] = 8
        data["analysis"] = {"state": "ready", "generation": 1, "proof": []}
        data["finality"] = {"state": "final", "reason": "budget_exhausted", "max_fragments": 1}
        call.status = "done"
        call.usage_json = {"homework": data}
        db.commit()
        calls = []
        def post(member_id, assignment_id, call_id):
            calls.append(call_id)
            if len(calls) == 1:
                raise b2b_client.HomeworkMaterialsError("homework_service_unavailable", recoverable=True)
            return {"outcome": "already_linked", "performed": True}
        monkeypatch.setattr(b2b_client, "conversation_result", post)
        now = datetime.now(timezone.utc)
        assert not deliver_homework(db, cid, now=now)
        assert not deliver_homework(db, cid, now=now+timedelta(seconds=29))
        assert deliver_homework(db, cid, now=now+timedelta(seconds=31))
        assert calls == [cid, cid]
        assert db.get(Call, cid).usage_json["homework"]["delivery"]["state"] == "acknowledged"


def test_new_session_recovers_persisted_delivery_without_memory_state(session_factory, seeded, monkeypatch):
    with session_factory() as db:
        cid = setup_call(db, seeded)
        call = db.get(Call, cid)
        namespace = dict(call.usage_json["homework"])
        namespace.update(assignment_id=8,
                         analysis={"state": "ready", "generation": 1, "proof": []},
                         finality={"state": "final", "reason": "budget_exhausted", "max_fragments": 1})
        call.status = "done"
        call.usage_json = {"homework": namespace}
        db.commit()
    requests = []

    def acknowledge(member_id, assignment_id, call_id, **kwargs):
        requests.append(call_id)
        return {"outcome": "already_linked", "performed": True}

    monkeypatch.setattr(b2b_client, "conversation_result", acknowledge)
    with session_factory() as fresh:
        assert recover_pending(fresh, member_id=seeded["member_id"] + 1) == 0
        assert recover_pending(fresh, member_id=seeded["member_id"]) == 1
        assert recover_pending(fresh, member_id=seeded["member_id"]) == 0
    assert requests == [cid]


def test_post_result_failure_does_not_revert_done_or_verified_ready(session_factory, seeded):
    with session_factory() as db:
        cid = setup_call(db, seeded)
        call = db.get(Call, cid)
        call.status = "done"
        db.commit()
        assert set_analysis_state(db, cid, (1, 1, 0), "failed", preserve_result=True)
        assert db.get(Call, cid).status == "done"
        assert commit_verified_analysis(db, cid, (1, 1, 0), [])
        assert not set_analysis_state(db, cid, (1, 1, 0), "failed", preserve_result=True)
        assert db.get(Call, cid).usage_json["homework"]["analysis"]["state"] == "ready"


def test_recovery_query_failure_is_contained_and_original_result_remains(session_factory, monkeypatch):
    with session_factory() as db:
        def unavailable(*a, **kw):
            raise RuntimeError("synthetic query failure")
        monkeypatch.setattr(db, "scalars", unavailable)
        assert recover_pending(db) == 0


def test_expired_recovery_budget_makes_no_http_request(session_factory, seeded, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("expired recovery issued HTTP")
    monkeypatch.setattr(b2b_client, "conversation_result", forbidden)
    with session_factory() as db:
        assert recover_pending(db, budget_s=0) == 0
