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
    CALL_DURATION_S_BY_PLAN,
    CALL_FRAGMENTS_BY_PLAN,
    CALL_FRAGMENT_S,
    call_fragments_for_member,
    DAILY_CALL_LIMIT,
    call_duration_s_for_member,
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


# --------------------------------------------------------------------------- #
# 4) 플랜별 혜택 — ⭐ 2026-08-19 재편: **길이가 아니라 조각 수**가 플랜을 가른다
# --------------------------------------------------------------------------- #
# 전: Free 한 통화 5분 / Pro·Max 한 통화 15분
# 후: 조각은 누구나 **6분**, Free 는 1개 / Pro·Max 는 3개(= 최대 18분)
#
# ⛔⛔ **앱 카피가 아직 옛 계약이다.** `app_en.arb` 의 Pro "Unlimited calls.
#    **15 minutes each**" 는 이제 사실이 아니다 — 한 번에 15분이 아니라 6분×3 이다.
#    이 시험의 원래 목적이 "앱 문구와 서버 값의 드리프트 잡기"였으므로, 숫자만 바꿔서
#    통과시키면 그 감시가 죽는다. ⇒ **프론트 문구 변경이 남은 일**임을 여기 남긴다.
#    (서버만 고쳐서는 못 닫는 구멍이다 — 결제한 사람이 보는 약속은 앱 화면에 있다.)
def test_the_plan_splits_on_fragment_count_not_length():
    """⭐ 조각 길이는 **플랜 무관 상수**이고, 플랜은 조각 수를 정한다."""
    assert CALL_FRAGMENT_S == 360.0
    assert CALL_DURATION_S_BY_PLAN[None] == CALL_FRAGMENT_S
    assert CALL_DURATION_S_BY_PLAN["pro"] == CALL_FRAGMENT_S
    assert CALL_DURATION_S_BY_PLAN["max"] == CALL_FRAGMENT_S

    assert CALL_FRAGMENTS_BY_PLAN[None] == 1     # Free — 한 조각
    assert CALL_FRAGMENTS_BY_PLAN["pro"] == 3    # 최대 18분
    assert CALL_FRAGMENTS_BY_PLAN["max"] == 3    # Pro 상위집합 — 길이는 같다


def test_the_fragment_is_longer_than_the_client_boundary():
    """⛔ 조각(6분)은 클라 경계(5분)보다 **넉넉해야** 한다.

    조각 경계는 **프론트가 정한다** — 5분에 "이어서 하시겠습니까?"를 띄우고 소켓을 닫는다.
    서버 시계는 그 뒤에 오는 **백스톱**이다. 5분으로 딱 맞추면 클라 지연·왕복 한 번에
    서버가 먼저 끊어 **비버 말이 잘린다.**
    """
    CLIENT_BOUNDARY_S = 300.0
    assert CALL_FRAGMENT_S > CLIENT_BOUNDARY_S


def test_the_fragment_never_outlives_the_absolute_backstop():
    """⛔⛔ 조각이 절대 백스톱을 밀어내면 **무한 과금 방어가 사라진다.**

    `absolute_timeout = max(ABSOLUTE_CALL_TIMEOUT_S, 조각 + SEED_TO_HANGUP_S + 30)`
    이므로, 조각이 커지면 어느 순간 백스톱이 **조각을 따라 늘어난다**. 6분에서는
    max(540, 360+22+30) = 540 으로 백스톱이 이긴다. 8분 이상이면 뒤집힌다.
    ⚠ 조각 길이를 올릴 땐 이 시험이 먼저 깨져야 한다.
    """
    from domains.learning.realtime.call_session import (
        ABSOLUTE_CALL_TIMEOUT_S, SEED_TO_HANGUP_S,
    )

    assert CALL_FRAGMENT_S + SEED_TO_HANGUP_S + 30.0 <= ABSOLUTE_CALL_TIMEOUT_S


@pytest.mark.parametrize(
    "state,plan,expected",
    [
        ("free", None, 1),
        ("active_pro", "pro", 3),
        ("active_max", "max", 3),
        # grace(결제 재시도 중)는 **접근 유지** — 혜택을 뺏으면 카드 갱신하는 동안
        # 통화가 짧아진다. ending(해지했지만 기간 남음)도 같다.
        ("grace", "max", 3),
        ("ending", "pro", 3),
        ("trial", "max", 3),
        # on_hold(유예도 끝남)·expired 는 접근 없음 → Free 혜택.
        ("on_hold", "max", 1),
        ("expired", None, 1),
    ],
)
def test_fragments_follow_effective_plan(monkeypatch, state, plan, expected):
    """⭐ **조각 수**가 구독 상태가 여는 플랜을 따른다 — resolve_status 를 재사용한다.

    ⚠ 2026-08-19 이전에는 이 시험의 축이 **길이**였다. 재편으로 길이가 상수가 되면서
      혜택을 나르는 값이 조각 수로 옮겨갔다 — 시험도 그 축으로 따라간다.
      ⛔ 지우지 않고 옮긴 이유: 지키려던 성질("결제 상태가 혜택을 연다")은 그대로다.
    """
    from domains.commerce.service.subscription_status import ResolvedStatus

    monkeypatch.setattr(
        "domains.commerce.service.subscription_status.resolve_status",
        lambda rows, **kw: ResolvedStatus(
            state=state, plan=plan, subscribe_id=1, price=None,
            start_date=None, end_date=None, retrying_until=None, paused_since=None,
        ),
    )
    monkeypatch.setattr(
        "domains.commerce.repository.subscribe_repository.SubscribeRepository",
        lambda _db: type("_R", (), {"list_by_member": lambda self, _m: [object()]})(),
    )
    assert call_fragments_for_member(None, 1) == expected


def test_fragments_fall_back_to_free_when_lookup_fails():
    """R5: 구독 조회가 죽어도 통화는 열린다 — 모르면 짧게(Free=1조각) 준다.

    db=None 이면 SubscribeRepository 가 터진다. 그게 여기서 원하는 상황이다.
    ⚠ 길이는 이제 상수라 이 자리에서 물을 게 없다 — 떨어질 수 있는 값은 조각 수뿐이다.
    """
    assert call_fragments_for_member(None, 1) == 1
    assert call_duration_s_for_member(None, 1) == CALL_FRAGMENT_S


@pytest.mark.parametrize("env", ["dev", "test", "prod"])
def test_duration_is_not_gated_by_env(monkeypatch, env):
    """⛔ 길이는 한도와 달리 환경으로 끄지 않는다.

    ENV 로 또 분기하면 dev 에서 플랜 경로가 한 번도 안 도는 죽은 코드가 된다.
    dev 에서 15분이 필요하면 NORMAL_CALL_DURATION_S 로 전 회원 강제하는 탈출구를 쓴다.
    """
    monkeypatch.setattr(app_settings, "ENV", env)
    assert call_duration_s_for_member(None, 1) == CALL_FRAGMENT_S
    assert call_fragments_for_member(None, 1) == 1
