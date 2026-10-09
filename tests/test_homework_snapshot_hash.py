import copy

import pytest

from core.homework_contract import snapshot_hash


def namespace():
    return {"version": 1, "assignment_id": 8, "source": "curriculum",
            "language": "ko", "snapshot": {"activities": ["conversation"],
            "grammar": ["N은/는"], "goals": [{"id": 1, "surface": " 가 "}],
            "attendance_targets": [{"id": 1}, {"id": 2}]}}


def test_key_order_and_delivery_changes_do_not_change_hash():
    original = namespace()
    changed = dict(reversed(list(original.items())))
    changed.update(analysis={"state": "ready"}, delivery={"state": "acknowledged"})
    assert snapshot_hash(original) == snapshot_hash(changed)
    assert len(snapshot_hash(original)) == 64


@pytest.mark.parametrize("change", [
    lambda d: d.update(assignment_id=9),
    lambda d: d["snapshot"]["goals"][0].update(surface="가"),
    lambda d: d["snapshot"]["goals"][0].update(meaning=None),
    lambda d: d["snapshot"]["attendance_targets"].reverse(),
    lambda d: d["snapshot"].update(attendance_targets=[{"id": 1}]),
])
def test_binding_raw_null_order_and_attendance_changes_are_detected(change):
    original = namespace()
    changed = copy.deepcopy(original)
    change(changed)
    assert snapshot_hash(original) != snapshot_hash(changed)


def test_missing_immutable_field_is_not_defaulted():
    data = namespace()
    del data["assignment_id"]
    with pytest.raises(KeyError):
        snapshot_hash(data)
