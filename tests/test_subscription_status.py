"""구독 상태 8종 판정 + GET /subscriptions/status 계약.

왜 이 테스트가 중요한가: 상태 판정이 틀리면 **해지 안내가 틀어진다** — 취소할 게
없는 사람에게 "스토어에서 취소하세요"를 보여주거나, Max 회원에게 Pro 화면을 보여준다.
판정은 순수 함수라 DB 없이 경계값을 전부 돌린다.

계약(응답 키·plan 동봉 규칙)은 앱 `SubscriptionStatusDto` 와 맞물려 있어 별도로 고정한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import pytest

from domains.commerce.service.subscription_status import resolve_status

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
FUTURE = NOW + timedelta(days=10)
PAST = NOW - timedelta(days=10)


@dataclass
class Row:
    """SubscribeRow 프로토콜을 만족하는 최소 더미(ORM 없이 판정만 검증)."""

    subscribe_id: int = 1
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    price: Optional[Decimal] = None
    is_activate: Optional[bool] = True
    plan: str = "pro"
    is_trial: bool = False
    billing_state: str = "ok"
    retrying_until: Optional[datetime] = None
    paused_since: Optional[datetime] = None


def _state(*rows: Row) -> str:
    return resolve_status(list(rows), now=NOW).state


# --------------------------------------------------------------------------- #
# 8종 기본 판정
# --------------------------------------------------------------------------- #
def test_no_rows_is_free():
    assert resolve_status([], now=NOW).state == "free"
    assert resolve_status([], now=NOW).plan is None


def test_active_pro_and_max_split_by_plan():
    assert _state(Row(plan="pro", end_date=FUTURE)) == "active_pro"
    assert _state(Row(plan="max", end_date=FUTURE)) == "active_max"


def test_trial_beats_plan():
    """체험은 별도 상태다 — 앱이 체험을 Max 로 취급하므로 plan 보다 먼저 본다."""
    assert _state(Row(plan="max", is_trial=True, end_date=FUTURE)) == "trial"


def test_grace_and_on_hold_beat_everything():
    """결제 사고 상태가 최우선 — 화면이 배너를 띄워야 한다."""
    assert _state(Row(billing_state="grace", end_date=FUTURE)) == "grace"
    assert _state(Row(billing_state="on_hold", end_date=FUTURE)) == "on_hold"
    # 체험 중 결제 실패도 결제 사고가 이긴다.
    assert _state(Row(is_trial=True, billing_state="grace", end_date=FUTURE)) == "grace"


def test_ending_is_cancelled_but_still_paid():
    """cancel 은 is_activate 만 내리고 end_date 는 남긴다 — 그 조합이 ENDING."""
    assert _state(Row(is_activate=False, end_date=FUTURE)) == "ending"


def test_expired_when_nothing_live():
    assert _state(Row(is_activate=True, end_date=PAST)) == "expired"
    assert _state(Row(is_activate=False, end_date=PAST)) == "expired"
    # is_activate 가 NULL 인 과거 행(컬럼에 서버 기본값이 없었다) = 접근 없음.
    assert _state(Row(is_activate=None, end_date=FUTURE)) == "expired"


def test_null_end_date_is_open_ended():
    """만료일이 없으면 무기한 활성 — iap_service.entitlement 와 같은 규칙."""
    assert _state(Row(end_date=None)) == "active_pro"


# --------------------------------------------------------------------------- #
# 경계값 — 만료 직전/직후
# --------------------------------------------------------------------------- #
def test_expiry_boundary_is_exclusive():
    """end_date == now 는 **만료**다(> 비교). 1초 뒤면 아직 살아 있다."""
    assert _state(Row(end_date=NOW)) == "expired"
    assert _state(Row(end_date=NOW + timedelta(seconds=1))) == "active_pro"


def test_naive_datetime_treated_as_utc():
    """DB 가 tz 를 잃어도 판정이 뒤집히지 않는다(iap_service._as_utc 와 같은 방어)."""
    naive_future = FUTURE.replace(tzinfo=None)
    assert _state(Row(end_date=naive_future)) == "active_pro"


# --------------------------------------------------------------------------- #
# 다중 행 — 앱 resolver 와 같은 규칙이어야 한다
# --------------------------------------------------------------------------- #
def test_newest_active_row_wins():
    """레거시 POST /subscriptions 가 중복 활성 행을 만들어 왔다. 최신이 이긴다 —
    앱 resolver 도 같은 규칙이라, 폴백 전후로 화면이 바뀌면 안 된다."""
    old = Row(subscribe_id=1, plan="pro", end_date=FUTURE)
    new = Row(subscribe_id=2, plan="max", end_date=FUTURE)
    assert _state(old, new) == "active_max"
    assert _state(new, old) == "active_max"  # 입력 순서와 무관


def test_active_beats_ending_regardless_of_order():
    ending = Row(subscribe_id=9, is_activate=False, end_date=FUTURE)
    active = Row(subscribe_id=2, is_activate=True, end_date=FUTURE)
    assert _state(ending, active) == "active_pro"


# --------------------------------------------------------------------------- #
# plan 동봉 규칙 — 빠지면 앱이 Pro 로 오판한다
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "row,expected_state",
    [
        (Row(plan="max", billing_state="grace", end_date=FUTURE), "grace"),
        (Row(plan="max", billing_state="on_hold", end_date=FUTURE), "on_hold"),
        (Row(plan="max", is_activate=False, end_date=FUTURE), "ending"),
        (Row(plan="max", is_trial=True, end_date=FUTURE), "trial"),
    ],
)
def test_plan_is_always_sent_for_lapsed_paid_states(row, expected_state):
    """⛔ grace·on_hold·ending·trial 에서 plan 이 빠지면 앱이 isPlanInferred 로
    **Pro 를 가정**한다 — Max 회원이 결제에 실패하면 Pro 화면을 보게 된다."""
    resolved = resolve_status([row], now=NOW)
    assert resolved.state == expected_state
    assert resolved.plan == "max"


def test_expired_carries_no_plan():
    """만료는 이미 Free 로 떨어진 회원 — 앱도 tier=free 로 본다."""
    assert resolve_status([Row(end_date=PAST)], now=NOW).plan is None


def test_retrying_and_paused_are_scoped_to_their_state():
    """다른 상태에 남은 잔여 값이 'Retrying until …' 배너를 띄우면 안 된다."""
    row = Row(end_date=FUTURE, retrying_until=FUTURE, paused_since=PAST)
    active = resolve_status([row], now=NOW)
    assert active.state == "active_pro"
    assert active.retrying_until is None and active.paused_since is None

    grace = resolve_status([Row(end_date=FUTURE, billing_state="grace",
                                retrying_until=FUTURE, paused_since=PAST)], now=NOW)
    assert grace.retrying_until == FUTURE and grace.paused_since is None

    hold = resolve_status([Row(end_date=FUTURE, billing_state="on_hold",
                               retrying_until=FUTURE, paused_since=PAST)], now=NOW)
    assert hold.paused_since == PAST and hold.retrying_until is None


# --------------------------------------------------------------------------- #
# 계약 — 앱 SubscriptionStatusDto 가 읽는 키 집합
# --------------------------------------------------------------------------- #
def test_response_keys_match_app_contract():
    """키를 바꾸면 앱이 파싱을 거부하고 구식 목록 추론으로 폴백한다."""
    from domains.commerce.schemas.subscription import SubscriptionStatusOut

    assert set(SubscriptionStatusOut.model_fields) == {
        "state", "plan", "subscribe_id", "price",
        "start_date", "end_date", "retrying_until", "paused_since",
    }


def test_state_literal_matches_app_enum():
    """앱은 이 8개 문자열만 파싱한다 — 모르는 값이면 폴백한다."""
    from typing import get_args

    from domains.commerce.schemas.subscription import SubscriptionStatusOut

    field = SubscriptionStatusOut.model_fields["state"]
    assert set(get_args(field.annotation)) == {
        "free", "trial", "active_pro", "active_max",
        "grace", "on_hold", "ending", "expired",
    }
