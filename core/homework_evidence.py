"""숙제 source별 원본 ID를 개인 학습 ID와 분리한 검증 어댑터."""
from __future__ import annotations

from domains.learning.service import normalcall_service as svc


class HomeworkKindBlocked(ValueError):
    """저장 근거 없는 manual 등급을 추정하지 않는다."""


def verification_candidates(homework: dict) -> tuple[list[dict], dict[int, dict]]:
    """전체 출제 대상을 임시 검증 번호로 변환한다. DB 조회 없음."""
    source = homework.get("source")
    if source == "manual":
        raise HomeworkKindBlocked("homework_kind_blocked")
    if source != "curriculum":
        raise ValueError("homework_binding_mismatch")
    targets = homework["snapshot"]["attendance_targets"]
    if not isinstance(targets, list) or not targets:
        raise ValueError("homework_snapshot_invalid")
    candidates, originals, seen = [], {}, set()
    for number, target in enumerate(targets, start=1):
        target_id = target.get("id")
        if (target.get("reference") != "learning_item"
                or type(target_id) is not int or target_id <= 0
                or target_id in seen
                or target.get("kind") not in {"vocab", "grammar", "chunk"}
                or not isinstance(target.get("surface"), str)
                or not target["surface"].strip()):
            raise ValueError("homework_snapshot_invalid")
        seen.add(target_id)
        originals[number] = dict(target)
        candidates.append({"item_id": number, "surface": target["surface"],
                           "kind": target["kind"], "example": target.get("example"),
                           "injected": False})
    return candidates, originals


def verify_homework_detections(homework: dict, detections: list,
                              dialog_rows: list[dict], through_turn_index: int,
                              hinted_from_turn_index: set[int] | None = None) -> list[dict]:
    """USER 인용을 기존 규칙으로 검증 후 원본 reference/id로 복원한다."""
    if type(through_turn_index) is not int or through_turn_index < -1:
        raise ValueError("homework_proof_invalid")
    candidates, originals = verification_candidates(homework)
    if through_turn_index == -1:
        if detections or dialog_rows:
            raise ValueError("homework_proof_invalid")
        return []
    if any(
        type(row.get("turn_index")) is not int or row["turn_index"] < 0
        for row in dialog_rows):
        raise ValueError("homework_proof_invalid")
    rows = [row for row in dialog_rows
            if type(row.get("turn_index")) is int
            and 0 <= row["turn_index"] <= through_turn_index]
    verified = svc._verify_detections(
        None, 0, 0, detections, candidates, rows,
        hinted_from_turn_index=hinted_from_turn_index,
    )
    result = []
    for evidence in verified:
        original = originals[evidence.item_id]
        result.append({"reference": original["reference"], "id": original["id"],
                       "turn_index": evidence.turn_index, "quote": evidence.quote,
                       "grade_raw": evidence.grade_raw, "grade_final": evidence.grade_final,
                       "verified": True, "norm_hash": evidence.norm_hash})
    return result
