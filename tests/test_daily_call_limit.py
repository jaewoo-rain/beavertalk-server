"""일일 통화 한도 — 서버가 통화 시작을 거절한다.

지시: 하루에 레벨테스트 1회 + 일반 통화 1회. 레벨테스트는 일반 통화 한도를 깎지 않는다.
근거: docs/20260729_1243_일일-통화-한도-서버-거절.md

⚠ 클라 게이팅은 우회 가능하므로 판정은 서버가 한다. 거절은 create_call·Live 세션 open
   **이전**에 일어나야 잔여물도 Gemini 비용도 안 생긴다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from core.config import settings as app_settings
from domains.learning.service.call_service import (
    DAILY_CALL_LIMIT,
    daily_window_utc,
    is_daily_limit_reached,
)


# --------------------------------------------------------------------------- #
# 1) 하루 경계 — 클라 타임존 기준
# --------------------------------------------------------------------------- #
def test_window_is_local_midnight_not_utc():
    """KST(+540) 7/29 하루 = UTC 7/28 15:00 ~ 7/29 15:00.

    서버가 UTC 로 고정하면 한국 사용자는 오전 9시에 날짜가 바뀐다.
    """
    s, e = daily_window_utc(date(2026, 7, 29), 540)
    assert s == datetime(2026, 7, 28, 15, tzinfo=timezone.utc)
    assert e == datetime(2026, 7, 29, 15, tzinfo=timezone.utc)
    assert e - s == timedelta(days=1)


def test_window_utc_when_offset_zero():
    s, e = daily_window_utc(date(2026, 7, 29), 0)
    assert s == datetime(2026, 7, 29, tzinfo=timezone.utc)
    assert e == datetime(2026, 7, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("offset", [-480, -60, 0, 330, 540, 840])
def test_window_is_always_exactly_one_day(offset):
    s, e = daily_window_utc(date(2026, 7, 29), offset)
    assert e - s == timedelta(days=1)


# --------------------------------------------------------------------------- #
# 2) 한도 판정 — 콜타입별 독립
# --------------------------------------------------------------------------- #
class _Repo:
    """has_call_in_window 호출을 기록하는 가짜 리포지토리."""

    def __init__(self, existing: set[str]):
        self.existing = existing
        self.calls: list[dict] = []

    def has_call_in_window(self, member_id, start_utc, end_utc, call_type=None):
        self.calls.append({
            "member_id": member_id, "start": start_utc, "end": end_utc,
            "call_type": call_type,
        })
        return call_type in self.existing


@pytest.fixture(autouse=True)
def _prod(monkeypatch):
    """한도는 prod 에서만 적용된다 — 판정 테스트는 prod 를 전제로 돈다."""
    monkeypatch.setattr(app_settings, "ENV", "prod")


@pytest.fixture
def patched_repo(monkeypatch):
    holder = {}

    def make(existing: set[str]):
        repo = _Repo(existing)
        monkeypatch.setattr(
            "domains.learning.service.call_service.CallRepository", lambda _db: repo
        )
        holder["repo"] = repo
        return repo

    make.holder = holder  # type: ignore[attr-defined]
    return make


def test_limits_are_one_each():
    """지시: 레벨테스트 1회 + 일반 통화 1회."""
    assert DAILY_CALL_LIMIT == {"normal": 1, "level_test": 1}


def test_normal_blocked_after_normal(patched_repo):
    patched_repo({"normal"})
    assert is_daily_limit_reached(None, 1, "normal", 540) is True


def test_level_test_not_blocked_by_normal(patched_repo):
    """★ 핵심: 일반 통화를 썼어도 레벨테스트는 남아 있다."""
    patched_repo({"normal"})
    assert is_daily_limit_reached(None, 1, "level_test", 540) is False


def test_normal_not_blocked_by_level_test(patched_repo):
    """★ 핵심: 레벨테스트를 썼어도 일반 통화는 남아 있다."""
    patched_repo({"level_test"})
    assert is_daily_limit_reached(None, 1, "normal", 540) is False


def test_both_used_blocks_both(patched_repo):
    patched_repo({"normal", "level_test"})
    assert is_daily_limit_reached(None, 1, "normal", 540) is True
    assert is_daily_limit_reached(None, 1, "level_test", 540) is True


def test_nothing_used_allows_both(patched_repo):
    patched_repo(set())
    assert is_daily_limit_reached(None, 1, "normal", 540) is False
    assert is_daily_limit_reached(None, 1, "level_test", 540) is False


def test_query_filters_by_call_type(patched_repo):
    """콜타입 필터가 실제로 쿼리에 실린다(안 실리면 한도가 서로를 깎는다)."""
    repo = patched_repo(set())
    is_daily_limit_reached(None, 7, "normal", 540)
    q = repo.calls[-1]
    assert q["call_type"] == "normal"
    assert q["member_id"] == 7
    assert q["end"] - q["start"] == timedelta(days=1)


# --------------------------------------------------------------------------- #
# 3) 환경 게이트 — prod 에서만 막는다
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("env", ["test", "dev", "staging", ""])
def test_not_limited_outside_prod(patched_repo, monkeypatch, env):
    """dev/test/데모는 자유 — 테스트하다 하루가 잠기면 개발이 안 된다."""
    monkeypatch.setattr(app_settings, "ENV", env)
    repo = patched_repo({"normal", "level_test"})
    assert is_daily_limit_reached(None, 1, "normal", 540) is False
    assert repo.calls == [], f"ENV={env} 인데 DB 를 조회했다(불필요한 왕복)"


def test_limited_in_prod(patched_repo, monkeypatch):
    monkeypatch.setattr(app_settings, "ENV", "prod")
    patched_repo({"normal"})
    assert is_daily_limit_reached(None, 1, "normal", 540) is True


def test_unknown_call_type_is_not_limited(patched_repo):
    """한도가 정의되지 않은 콜타입은 막지 않는다(새 콜타입이 조용히 잠기지 않게)."""
    patched_repo({"normal", "level_test"})
    assert is_daily_limit_reached(None, 1, "practice", 540) is False
