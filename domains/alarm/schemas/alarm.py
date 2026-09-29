"""alarm 관련 DTO. schedule(반복요일)을 days_of_week 배열로 평탄화해서 다룬다."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from domains.learning.service.call_service import _resolve_zone

# 요일 화이트리스트 — 잘못된 값은 422 로 거부됨
DayOfWeek = Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

# ⛔⛔ §25-①(2026-09-28, 출시 전 권장) — 범위 밖(|값|≥1440)이면 dispatch_service.
#   _offset_zone 의 datetime.timezone(timedelta(...)) 이 ValueError 로 죽는다.
#   call_service.py 의 tz_offset_min 검증(-14*60~14*60)과 같은 값 — 그쪽과 어긋나면
#   "이 화면은 되는데 알람은 안 된다"가 된다.
_TZ_OFFSET_MIN, _TZ_OFFSET_MAX = -14 * 60, 14 * 60


def _validate_tz(v: Optional[str]) -> Optional[str]:
    """⛔⛔ (2026-09-29) 알람 tz 는 **유효한 IANA 만** 저장한다 — 아니면 422.

    이유: 앱 빌드 45 부터 이 값을 실제로 보낸다. 오타가 조용히 저장되면 디스패치가 경고만
    남기고 폴백하는 동안 그 회원은 **엉뚱한 시각에 전화를 받는데** 응답은 200 이라 아무도
    모른다. §25-① 의 tz_offset_min 범위 검사와 같은 축이다.
    - 판정은 디스패치와 **같은 함수**(call_service._resolve_zone)다 — 두 곳이 어긋나면
      「저장은 됐는데 디스패치가 폴백」이 또 생긴다. 저장값은 정규 이름(zone.key).
    - None·빈 문자열(공백만 포함)은 «미지정»으로 None — 422 가 아니다(기존 NULL 알람과 같다.
      update 에선 None 이면 그 필드를 안 건드린다).
    ⚠ 디스패치 폴백 체인(member.tz → alarm.tz → offset → 서울)은 그대로다 — 이 검사 이전에
      저장된 값은 여전히 잘못될 수 있다.
    """
    if v is None or not v.strip():
        return None
    zone = _resolve_zone(v.strip()) if len(v) <= 64 else None
    if zone is None:
        raise ValueError(f"tz 는 IANA 시간대 이름이어야 합니다(예: Asia/Seoul): {v!r}")
    return zone.key


class AlarmCharacterBrief(BaseModel):
    character_id: int
    name: str
    image_url: Optional[str]


CallType = Literal["auto", "chat"]


# ── 요청 ──
class AlarmCreate(BaseModel):
    character_id: int
    time: datetime
    is_activate: bool = True
    days_of_week: list[DayOfWeek]
    call_type: CallType = "auto"  # 키 없으면 학습(auto)
    # §5(2026-09-28) — 알람 시간대. 둘 다 생략 가능(NULL=미상, 디스패치가 서울로
    # 폴백한다). ⛔ 여기서 서울을 기본값으로 채우지 않는다 — "안 보냄"과 "서울"이
    # 구분돼야 한다.
    tz: Optional[str] = None
    tz_offset_min: Optional[int] = Field(default=None, ge=_TZ_OFFSET_MIN, le=_TZ_OFFSET_MAX)

    _tz_is_iana = field_validator("tz")(_validate_tz)


class AlarmUpdate(BaseModel):
    """전체 수정. days_of_week 를 주면 기존 요일을 통째로 교체."""

    time: Optional[datetime] = None
    character_id: Optional[int] = None
    is_activate: Optional[bool] = None
    days_of_week: Optional[list[DayOfWeek]] = None
    call_type: Optional[CallType] = None
    tz: Optional[str] = None
    tz_offset_min: Optional[int] = Field(default=None, ge=_TZ_OFFSET_MIN, le=_TZ_OFFSET_MAX)

    _tz_is_iana = field_validator("tz")(_validate_tz)


# ── 응답 ──
class AlarmOut(BaseModel):
    alarm_id: int
    time: Optional[datetime]
    is_activate: Optional[bool]
    character: AlarmCharacterBrief
    days_of_week: list[str]
    call_type: str
    # ⛔ 저장값 그대로 — 없는 시간대를 서울 등으로 지어내 내보내지 않는다.
    tz: Optional[str]
    tz_offset_min: Optional[int]
