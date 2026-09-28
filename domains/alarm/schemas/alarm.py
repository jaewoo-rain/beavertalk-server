"""alarm 관련 DTO. schedule(반복요일)을 days_of_week 배열로 평탄화해서 다룬다."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

# 요일 화이트리스트 — 잘못된 값은 422 로 거부됨
DayOfWeek = Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


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
    tz_offset_min: Optional[int] = None


class AlarmUpdate(BaseModel):
    """전체 수정. days_of_week 를 주면 기존 요일을 통째로 교체."""

    time: Optional[datetime] = None
    character_id: Optional[int] = None
    is_activate: Optional[bool] = None
    days_of_week: Optional[list[DayOfWeek]] = None
    call_type: Optional[CallType] = None
    tz: Optional[str] = None
    tz_offset_min: Optional[int] = None


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
