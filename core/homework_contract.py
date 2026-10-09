"""숙제 불변 스냅샷 해시. 검증 결과·배송 상태와 분리한다."""
from __future__ import annotations

import hashlib
import json
import copy


SNAPSHOT_FIELDS = ("version", "assignment_id", "source", "language", "snapshot")


def create_namespace(materials: dict) -> dict:
    from core.prompts.homework import normalize_homework
    data = normalize_homework(materials)
    snapshot = {key: copy.deepcopy(data[key]) for key in ("activities", "grammar", "goals")}
    if "attendance_targets" in data:
        snapshot["attendance_targets"] = copy.deepcopy(data["attendance_targets"])
    result = {"version": 1, "assignment_id": data["assignment_id"],
              "source": data["source"], "language": data["language"], "snapshot": snapshot,
              "runtime": {"generation": 1, "fragment_count": 1, "active": True,
                          "through_turn_index": -1},
              "analysis": {"state": "pending", "verification_version": "homework-v1", "proof": []},
              "delivery": {"state": "pending", "generation": 1, "attempts": 0},
              "finality": {"state": "awaiting_resume"}}
    result["snapshot_hash"] = snapshot_hash(result)
    return result


def namespace_materials(namespace: dict) -> dict:
    if not isinstance(namespace, dict) or any(key not in namespace for key in SNAPSHOT_FIELDS):
        raise ValueError("homework_snapshot_invalid")
    if namespace.get("version") != 1 or namespace.get("snapshot_hash") != snapshot_hash(namespace):
        raise ValueError("homework_snapshot_invalid")
    return {"assignment_id": namespace["assignment_id"], "source": namespace["source"],
            "language": namespace["language"], **copy.deepcopy(namespace["snapshot"])}


def snapshot_hash(namespace: dict) -> str:
    """양측 합의한 다섯 필드의 canonical UTF-8 SHA256."""
    body = {field: namespace[field] for field in SNAPSHOT_FIELDS}
    encoded = json.dumps(
        body, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
