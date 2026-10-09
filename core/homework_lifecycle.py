"""숙제 JSON 상태 전이. 호출자는 Call 행 잠금 안에서 적용한다."""
from __future__ import annotations

import copy
from datetime import datetime, timezone


def resume_namespace(namespace: dict, fragment_count: int) -> dict:
    data = copy.deepcopy(namespace)
    runtime = data["runtime"]
    if fragment_count != runtime["fragment_count"] + 1:
        raise ValueError("homework_generation_mismatch")
    runtime.update(generation=runtime["generation"] + 1,
                   fragment_count=fragment_count, active=True)
    data["analysis"].update(state="pending")
    data["delivery"].update(state="pending", generation=runtime["generation"])
    data["finality"] = {"state": "awaiting_resume"}
    return data


def analysis_cas_matches(namespace: dict, expected: tuple[int, int, int],
                         actual_fragment_count: int, actual_active: bool,
                         actual_through_turn_index: int) -> bool:
    runtime = namespace["runtime"]
    return (
        (runtime["generation"], runtime["fragment_count"], runtime["through_turn_index"]) == expected
        and runtime["fragment_count"] == actual_fragment_count
        and runtime["active"] is False and actual_active is False
        and runtime["through_turn_index"] == actual_through_turn_index
        and actual_through_turn_index >= -1
    )


def ttl_finality_ready(namespace: dict, *, fragment_ended_at: datetime | None,
                       actual_active: bool, now: datetime) -> bool:
    """300초 정확 경계는 기존 resume 허용 범위이므로 귀속하지 않는다."""
    if actual_active or namespace["runtime"]["active"] or fragment_ended_at is None:
        return False
    ended = fragment_ended_at
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    return (now - ended).total_seconds() > 300
