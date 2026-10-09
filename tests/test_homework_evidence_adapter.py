import copy

import pytest

from core.homework_evidence import (
    HomeworkKindBlocked, verification_candidates, verify_homework_detections,
)
from domains.learning.service.normalcall_service import ItemDetection


def homework():
    return {"source": "curriculum", "snapshot": {
        "goals": [{"reference": "learning_item", "id": 51}],
        "attendance_targets": [
            {"reference": "learning_item", "id": 51, "surface": "의사", "kind": "vocab"},
            {"reference": "learning_item", "id": 99, "surface": "안녕하세요", "kind": "chunk"},
        ]}}


def test_all_attendance_targets_and_original_ids_are_preserved():
    data = homework()
    before = copy.deepcopy(data)
    candidates, originals = verification_candidates(data)
    assert [c["item_id"] for c in candidates] == [1, 2]
    assert [originals[i]["id"] for i in (1, 2)] == [51, 99]
    rows = [{"role": "user", "turn_index": 4, "content": "안녕하세요"}]
    proof = verify_homework_detections(data, [
        ItemDetection(item_id=2, evidence="E2", quote="안녕하세요")], rows, 4)
    assert proof[0]["id"] == 99  # goals 밖 수행 증거를 제거하지 않는다.
    assert proof[0]["reference"] == "learning_item"
    assert proof[0]["grade_final"] == "E2"  # 실제 chunk 분류를 사용한다.
    assert data == before


def test_real_source_id_cannot_be_used_as_verification_number():
    proof = verify_homework_detections(homework(), [
        ItemDetection(item_id=99, evidence="E2", quote="안녕하세요")],
        [{"role": "user", "turn_index": 4, "content": "안녕하세요"}], 4)
    assert proof == []


def test_future_fragment_transcript_is_excluded():
    proof = verify_homework_detections(homework(), [
        ItemDetection(item_id=2, evidence="E2", quote="안녕하세요")],
        [{"role": "user", "turn_index": 5, "content": "안녕하세요"}], 4)
    assert proof == []


def test_manual_kind_is_blocked_without_grade_guess():
    data = homework()
    data["source"] = "manual"
    with pytest.raises(HomeworkKindBlocked):
        verification_candidates(data)


def test_empty_transcript_minus_one_is_normal_empty_proof():
    assert verify_homework_detections(homework(), [], [], -1) == []


@pytest.mark.parametrize("rows,detections,through", [
    ([{"role": "user", "content": "안녕하세요", "turn_index": 0}], [], -1),
    ([], [ItemDetection(item_id=2, evidence="E1", quote="안녕하세요")], -1),
    ([{"role": "user", "content": "안녕하세요"}], [], 3),
    ([{"role": "user", "content": "안녕하세요", "turn_index": -1}], [], 3),
    ([{"role": "user", "content": None, "turn_index": None}], [], 3),
    ([{"role": "user", "content": None, "turn_index": None}], [], -1),
])
def test_invalid_empty_range_or_unnumbered_transcript_is_not_hidden(rows, detections, through):
    with pytest.raises(ValueError):
        verify_homework_detections(homework(), detections, rows, through)


@pytest.mark.parametrize("change", [
    lambda d: d["snapshot"]["attendance_targets"][1].update(id=51),
    lambda d: d["snapshot"]["attendance_targets"][0].update(kind=None),
    lambda d: d["snapshot"]["attendance_targets"][0].update(reference="assignment_item"),
])
def test_invalid_kind_source_and_duplicate_are_rejected(change):
    data = homework()
    change(data)
    with pytest.raises(ValueError):
        verification_candidates(data)
