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


# ── 요청 ──
class AlarmCreate(BaseModel):
    character_id: int
    time: datetime
    is_activate: bool = True
    days_of_week: list[DayOfWeek]


class AlarmUpdate(BaseModel):
    """전체 수정. days_of_week 를 주면 기존 요일을 통째로 교체."""

    time: Optional[datetime] = None
    character_id: Optional[int] = None
    is_activate: Optional[bool] = None
    days_of_week: Optional[list[DayOfWeek]] = None


# ── 응답 ──
class AlarmOut(BaseModel):
    alarm_id: int
    time: Optional[datetime]
    is_activate: Optional[bool]
    character: AlarmCharacterBrief
    days_of_week: list[str]
