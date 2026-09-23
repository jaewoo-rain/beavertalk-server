"""stats 라우터 — 학습 달력(calendar) API. C12(2026-09-23, D8·D9)."""

from __future__ import annotations

from fastapi import APIRouter

from core.deps import CurrentMember, DbSession
from domains.learning.service.call_service import CallService

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/calendar")
def get_calendar(
    start: str, end: str, member: CurrentMember, db: DbSession,
    tz: str | None = None, tz_offset_min: int | None = None,
) -> dict:
    """학습 달력 — [start, end](YYYY-MM-DD, 포함) 로컬 날짜 범위의 통화 집계.

    - tz: IANA 존 이름("Asia/Seoul"). 있으면 tz_offset_min 보다 우선(서머타임 안전).
    - tz_offset_min: tz 가 없거나 잘못됐을 때의 폴백(분, 동쪽 +). 둘 다 없으면 UTC.
    - start>end 또는 범위가 400일을 넘으면 422.

    집계 대상은 본인의 **성립 통화**(학습자가 최소 한 번 말한 done/analyzing 통화,
    레벨테스트 제외) — `daily-status` 의 `called_today` 와 같은 판정 기준
    (`has_call_in_window`)을 재사용한다.

    ```
    {
      "days": [{"date":"2026-09-22","sentences":6,"words":132,"call_count":2,"call_minutes":14}],
      "total": {"sentences":…, "words":…, "call_count":…, "call_days":…, "call_minutes":…}
    }
    ```
    통화가 없는 날은 `days` 에 없다. `words` 가 그 날/그 기간에 전혀 집계 안 됐으면
    (사용자 전사가 없던 통화만 있었으면) `words` 키 자체가 빠진다(0 이 아니다).
    """
    return CallService(db).get_calendar(
        member.member_id, start, end, tz=tz, tz_offset_min=tz_offset_min,
    )
