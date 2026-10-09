from datetime import datetime, timedelta, timezone

import pytest

from core.homework_lifecycle import analysis_cas_matches, resume_namespace, ttl_finality_ready


def namespace():
    return {"runtime": {"generation": 1, "fragment_count": 1,
                        "active": False, "through_turn_index": 5},
            "analysis": {"state": "ready", "proof": []},
            "delivery": {"state": "acknowledged", "generation": 1},
            "finality": {"state": "awaiting_resume"}}


def test_resume_invalidates_old_analysis_and_delivery_without_mutating_snapshot():
    old = namespace()
    new = resume_namespace(old, 2)
    assert new["runtime"]["generation"] == 2
    assert new["runtime"]["active"] is True
    assert new["analysis"]["state"] == "pending"
    assert new["delivery"] == {"state": "pending", "generation": 2}
    assert old["analysis"]["state"] == "ready"
    assert not analysis_cas_matches(new, (1, 1, 5), 2, True, 5)


@pytest.mark.parametrize("actual", [(2, False, 5), (1, True, 5), (1, False, 6)])
def test_changed_fragment_active_or_transcript_rejects_analysis(actual):
    assert not analysis_cas_matches(namespace(), (1, 1, 5), *actual)


def test_current_ended_generation_accepts_analysis_and_empty_transcript_minus_one():
    assert analysis_cas_matches(namespace(), (1, 1, 5), 1, False, 5)
    data = namespace()
    data["runtime"]["through_turn_index"] = -1
    assert analysis_cas_matches(data, (1, 1, -1), 1, False, -1)


@pytest.mark.parametrize("elapsed,expected", [(299, False), (300, False), (301, True)])
def test_ttl_strict_boundary(elapsed, expected):
    now = datetime.now(timezone.utc)
    assert ttl_finality_ready(namespace(), fragment_ended_at=now-timedelta(seconds=elapsed),
                              actual_active=False, now=now) is expected


def test_missing_end_and_active_fragment_cannot_be_attributed_by_ttl():
    now = datetime.now(timezone.utc)
    assert not ttl_finality_ready(namespace(), fragment_ended_at=None, actual_active=False, now=now)
    assert not ttl_finality_ready(namespace(), fragment_ended_at=now-timedelta(seconds=400),
                                  actual_active=True, now=now)


def test_fragment_skip_cannot_increment_generation():
    with pytest.raises(ValueError):
        resume_namespace(namespace(), 3)
