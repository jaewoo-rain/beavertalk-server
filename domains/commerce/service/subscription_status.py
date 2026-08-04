"""구독 행 → 상태 8종 판정 (순수 함수, DB·시계 의존 없음).

왜 별도 모듈인가: 상태 판정이 틀리면 **해지 안내가 틀어진다** — 이 도메인에서 가장
비싼 오류다("취소하려면 스토어로 가세요"를 취소할 게 없는 사람에게 보여주는 식).
그래서 판정만 떼어내 DB 없이 경계값을 전부 테스트할 수 있게 한다.

⛔ 앱(flutter `SubscriptionStatusResolver`)과 **같은 규칙**이어야 한다. 앱은 이
   엔드포인트가 404 면 행 목록 추론으로 폴백하는데, 두 규칙이 다르면 폴백 전후로
   화면이 바뀐다. 특히 "활성 행이 여럿이면 가장 최근 것이 이긴다"가 그렇다.

계획: docs/20260804_2353_구독-3티어-재편-구현계획.md
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Protocol, Sequence


class SubscribeRow(Protocol):
    """판정에 필요한 필드만. Subscribe ORM 모델이 이 모양을 만족한다."""

    subscribe_id: int
    start_date: Optional[datetime]
    end_date: Optional[datetime]
    price: Optional[Decimal]
    is_activate: Optional[bool]
    plan: str
    is_trial: bool
    billing_state: str
    retrying_until: Optional[datetime]
    paused_since: Optional[datetime]


@dataclass(frozen=True)
class ResolvedStatus:
    state: str
    plan: Optional[str]
    subscribe_id: Optional[int]
    price: Optional[Decimal]
    start_date: Optional[datetime]
    end_date: Optional[datetime]
    retrying_until: Optional[datetime]
    paused_since: Optional[datetime]


FREE = ResolvedStatus(
    state="free", plan=None, subscribe_id=None, price=None,
    start_date=None, end_date=None, retrying_until=None, paused_since=None,
)


def _as_utc(dt: datetime) -> datetime:
    """naive datetime 을 UTC 로 간주(DB 가 tz 를 잃는 경우 대비 — iap_service 와 동일)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _is_live(row: SubscribeRow, now: datetime) -> bool:
    """만료 시각이 아직 안 지났나. end_date 가 없으면 무기한으로 본다."""
    end = row.end_date
    return end is None or _as_utc(end) > now


def resolve_status(
    rows: Sequence[SubscribeRow], *, now: Optional[datetime] = None
) -> ResolvedStatus:
    """구독 행들 → 현재 상태 1건.

    판정 순서(위가 이긴다):
      1. 활성(is_activate ∧ 미만료) 중 가장 최근 행
           on_hold  → billing_state == 'on_hold'   (접근 차단)
           grace    → billing_state == 'grace'     (접근 유지)
           trial    → is_trial
           active_max / active_pro → plan
      2. 해지했으나 기간 남음(is_activate=False ∧ 미만료) → ending
      3. 행은 있으나 전부 실효 → expired
      4. 행 없음 → free

    ⚠ is_activate 가 NULL 인 행은 "한 번도 활성화된 적 없음"으로 본다(컬럼에 서버
      기본값이 없어 과거 행에 NULL 이 있다). 접근 없음이라는 점에서 만료와 같다.
    """
    if not rows:
        return FREE

    now = now or datetime.now(timezone.utc)

    # 최신 우선 — 호출부가 정렬을 보장하지 않아도 되게 여기서 정렬한다.
    ordered = sorted(rows, key=lambda r: r.subscribe_id, reverse=True)

    for row in ordered:
        if row.is_activate is not True or not _is_live(row, now):
            continue
        if row.billing_state == "on_hold":
            state = "on_hold"
        elif row.billing_state == "grace":
            state = "grace"
        elif row.is_trial:
            state = "trial"
        elif row.plan == "max":
            state = "active_max"
        else:
            state = "active_pro"
        return _from_row(row, state)

    for row in ordered:
        # 해지했지만 결제한 기간이 남았다 — cancel 은 is_activate 만 내리고
        # end_date 는 그대로 두므로, 이 조합이 정확히 ENDING 이다.
        if row.is_activate is False and _is_live(row, now):
            return _from_row(row, "ending")

    return _from_row(_latest_end(ordered), "expired")


def _from_row(row: SubscribeRow, state: str) -> ResolvedStatus:
    """행 + 상태 → 응답. plan 은 free/expired 를 뺀 전 상태에서 반드시 채운다.

    expired 는 이미 Free 로 떨어진 회원이라 플랜이 없다 — 앱도 tier=free 로 본다.
    """
    return ResolvedStatus(
        state=state,
        plan=None if state == "expired" else (row.plan or "pro"),
        subscribe_id=row.subscribe_id,
        price=row.price,
        start_date=row.start_date,
        end_date=row.end_date,
        # 해당 상태에서만 의미가 있다 — 다른 상태에 남은 잔여 값이 화면에
        # "Retrying until …" 을 띄우지 않게 여기서 잘라낸다.
        retrying_until=row.retrying_until if state == "grace" else None,
        paused_since=row.paused_since if state == "on_hold" else None,
    )


def _latest_end(ordered: Sequence[SubscribeRow]) -> SubscribeRow:
    """만료 표시에 쓸 행 — end_date 가 가장 늦은 것(없으면 가장 최근 행)."""
    dated = [r for r in ordered if r.end_date is not None]
    if not dated:
        return ordered[0]
    return max(dated, key=lambda r: _as_utc(r.end_date))  # type: ignore[arg-type]
