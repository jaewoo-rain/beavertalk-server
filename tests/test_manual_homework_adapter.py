import copy

import pytest

from core.homework_contract import create_namespace, namespace_materials, snapshot_hash
from core.homework_evidence import HomeworkKindBlocked, verification_candidates, verify_homework_detections
from domains.learning.service.normalcall_service import ItemDetection
from test_normalcall_ws import session_factory, seeded, _mock_external


def manual(kind="sentence", surface="저는 의사예요"):
    goal = dict(reference="assignment_item", id=73, surface=surface, kind=None, conversation=True)
    material = dict(assignment_id=8, source="manual", language="ko", activities=["conversation"],
                    grammar=[], goals=[goal], attendance_targets=[goal])
    ns = create_namespace(material)
    ns["version"] = 2
    ns["snapshot"]["manual_kind"] = kind
    ns["analysis"]["verification_version"] = "homework-v2"
    ns["snapshot_hash"] = snapshot_hash(ns)
    return ns


def verify(ns, quote, grade="E2", prior=None, hints=None, number=1):
    rows = ([dict(role="beaver", content=prior, turn_index=0)] if prior else [])
    rows.append(dict(role="user", content=quote, turn_index=1))
    return verify_homework_detections(ns, [ItemDetection(item_id=number, evidence=grade, quote=quote)], rows, 1, hints)


@pytest.mark.parametrize("kind,expected", [("word", "E1"), ("sentence", "E2")])
def test_solo_exception_stays_in_manual_adapter_without_mutating_canonical(kind, expected):
    ns = manual(kind)
    before = copy.deepcopy(ns)
    proof = verify(ns, "저는 의사예요")
    assert proof[0]["grade_final"] == expected
    assert (proof[0]["id"], proof[0]["reference"]) == (73, "assignment_item")
    assert ns == before
    assert ns["snapshot"]["attendance_targets"][0]["kind"] is None


def test_word_context_uses_existing_vocab_rules():
    assert verify(manual("word", "의사"), "저는 의사예요")[0]["grade_final"] == "E2"


def test_short_sentence_has_no_new_exception():
    assert verify(manual("sentence", "안녕"), "안녕")[0]["grade_final"] == "E1"


def test_existing_echo_and_hint_demotion():
    ns = manual()
    assert verify(ns, "저는 의사예요", "E3", prior="저는 의사예요")[0]["grade_final"] == "E1"
    assert verify(ns, "저는 의사예요", "E2", prior="저는 의사예요")[0]["grade_final"] == "E2"
    assert verify(ns, "저는 의사예요", hints={1})[0]["grade_final"] == "E1"


@pytest.mark.parametrize("version,kind", [(1, "word"), (1, "sentence"), (2, None)])
def test_old_version_and_unknown_classification_are_not_regraded(version, kind):
    ns = manual(kind)
    ns["version"] = version
    ns["snapshot_hash"] = snapshot_hash(ns)
    with pytest.raises(HomeworkKindBlocked):
        verification_candidates(ns)


def test_tamper_or_future_version_is_rejected():
    ns = manual()
    ns["snapshot"]["manual_kind"] = "word"
    with pytest.raises(ValueError):
        namespace_materials(ns)
    ns["version"] = 3
    ns["snapshot_hash"] = snapshot_hash(ns)
    with pytest.raises(ValueError):
        namespace_materials(ns)


def test_original_id_is_not_a_candidate_number_and_quote_must_be_real_user():
    ns = manual()
    assert verify(ns, "저는 의사예요", number=73) == []
    assert verify_homework_detections(ns, [ItemDetection(item_id=1, evidence="E2", quote="저는 의사예요")],
        [dict(role="user", content="다른 발화", turn_index=1)], 1) == []


def test_duplicate_quotes_and_future_turns_keep_existing_boundaries():
    ns = manual()
    detection = ItemDetection(item_id=1, evidence="E2", quote="저는 의사예요")
    rows = [dict(role="user", content="저는 의사예요", turn_index=1)]
    assert len(verify_homework_detections(ns, [detection, detection], rows, 1)) == 1
    assert verify_homework_detections(ns, [detection], rows, 0) == []


def test_read_compatibility_does_not_enable_new_snapshot_writer():
    payload = namespace_materials(manual())
    assert create_namespace(payload)["version"] == 1


def test_enabled_writer_is_only_for_explicit_manual_kind_and_policy_matches():
    payload = namespace_materials(manual())
    ns = create_namespace(payload, manual_v2_enabled=True)
    assert ns["version"] == 2
    assert ns["analysis"]["verification_version"] == "homework-v2"
    assert namespace_materials(ns) == payload
    payload["manual_kind"] = None
    assert create_namespace(payload, manual_v2_enabled=True)["version"] == 1
    ns["analysis"]["verification_version"] = "homework-v1"
    with pytest.raises(HomeworkKindBlocked):
        verification_candidates(ns)


def test_writer_gate_flip_preserves_existing_v1_and_v2_hash(session_factory, seeded, monkeypatch):
    from core.config import settings
    from domains.learning.models.call import Call
    from domains.learning.service import normalcall_service as svc
    payload = namespace_materials(manual())
    with session_factory() as db:
        for initially_enabled in (False, True):
            monkeypatch.setattr(settings, "HOMEWORK_MANUAL_V2_WRITE_ENABLED", initially_enabled)
            cid = svc.create_call(db, seeded["member_id"], seeded["character_id"], "homework", homework=payload)
            before = copy.deepcopy(db.get(Call, cid).usage_json)
            monkeypatch.setattr(settings, "HOMEWORK_MANUAL_V2_WRITE_ENABLED", not initially_enabled)
            svc.save_homework_snapshot(db, cid, payload)
            assert db.get(Call, cid).usage_json == before


def test_v2_analysis_persists_source_proof_policy_without_personal_progress(session_factory, seeded, monkeypatch):
    from core.config import settings
    from domains.learning.models.call import Call
    from domains.learning.models.call_raw_data import CallRawData
    from domains.learning.models.member_item_progress import MemberItemProgress
    from domains.learning.service import normalcall_service as svc
    from domains.learning.service.homework_service import prepare_analysis, commit_verified_analysis
    payload = namespace_materials(manual())
    monkeypatch.setattr(settings, "HOMEWORK_MANUAL_V2_WRITE_ENABLED", True)
    with session_factory() as db:
        personal_before = db.query(MemberItemProgress).count()
        cid = svc.create_call(db, seeded["member_id"], seeded["character_id"], "homework", homework=payload)
        old_hash = db.get(Call, cid).usage_json["homework"]["snapshot_hash"]
        db.add(CallRawData(call_id=cid, role="user", content="저는 의사예요", turn_index=0))
        db.commit()
        svc.mark_fragment_ended(db, cid)
        assert prepare_analysis(db, cid, 1, 1, 1) == (1, 1, 0)
        assert commit_verified_analysis(db, cid, (1, 1, 0),
            [ItemDetection(item_id=1, evidence="E2", quote="저는 의사예요")])
        data = db.get(Call, cid).usage_json["homework"]
        assert data["snapshot_hash"] == old_hash
        assert data["analysis"]["verification_version"] == "homework-v2"
        assert data["analysis"]["proof"][0]["reference"] == "assignment_item"
        assert data["analysis"]["proof"][0]["id"] == 73
        assert db.query(MemberItemProgress).count() == personal_before


def test_full_attendance_outside_selected_goals_preserves_order_and_rights_kind():
    payload = namespace_materials(manual())
    payload["goals"][0]["kind"] = "vocab"
    payload["attendance_targets"][0]["kind"] = "vocab"
    payload["attendance_targets"].append(dict(reference="assignment_item", id=91,
        surface="오늘 날씨가 좋아요", kind="vocab", conversation=True))
    ns = create_namespace(payload, manual_v2_enabled=True)
    before = copy.deepcopy(ns)
    candidates, originals = verification_candidates(ns)
    assert [originals[index]["id"] for index in (1, 2)] == [73, 91]
    proof = verify(ns, "오늘 날씨가 좋아요", number=2)
    assert proof[0]["id"] == 91
    assert proof[0]["grade_final"] == "E2"
    assert ns == before
    assert ns["snapshot"]["goals"][0]["kind"] == "vocab"


@pytest.mark.parametrize("mutation", [
    lambda payload: payload.update(manual_kind="mixed"),
    lambda payload: payload["attendance_targets"][0].update(reference="learning_item"),
    lambda payload: payload["attendance_targets"][0].update(conversation=False),
    lambda payload: payload["attendance_targets"][0].update(surface="다른 원문"),
])
def test_new_manual_contract_mismatch_is_rejected_without_inference(mutation):
    payload = namespace_materials(manual())
    mutation(payload)
    with pytest.raises(ValueError):
        create_namespace(payload, manual_v2_enabled=True)
