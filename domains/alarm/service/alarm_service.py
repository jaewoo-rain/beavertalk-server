"""AlarmService — 알람 + 반복요일(schedule) 동시 관리.

- 생성/수정 시 schedule 을 자식 배열로 함께 처리(한 트랜잭션)
- 요일 교체는 cascade='all, delete-orphan' 으로 기존 schedule 자동 삭제(= JPA orphanRemoval)
- 모든 작업은 소유자 검증(내 알람이 아니면 404)
"""

from __future__ import annotations

from typing import Sequence

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from domains.alarm.models.alarm import Alarm
from domains.alarm.models.schedule import Schedule
from domains.alarm.repository.alarm_repository import AlarmRepository
from domains.alarm.schemas.alarm import (
    AlarmCharacterBrief,
    AlarmCreate,
    AlarmOut,
    AlarmUpdate,
)


class AlarmService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = AlarmRepository(db)

    def list(self, member_id: int) -> list[AlarmOut]:
        return [self._to_out(a) for a in self.repo.list_by_member(member_id)]

    def get(self, member_id: int, alarm_id: int) -> AlarmOut:
        return self._to_out(self._get_owned(member_id, alarm_id))

    def create(self, member_id: int, data: AlarmCreate) -> AlarmOut:
        alarm = Alarm(
            member_id=member_id,
            character_id=data.character_id,
            time=data.time,
            is_activate=data.is_activate,
            schedules=[Schedule(day_of_week=d) for d in data.days_of_week],
        )
        self.repo.add(alarm)
        self.db.commit()  # 알람 + 반복요일 한 트랜잭션
        self.db.refresh(alarm)
        return self._to_out(alarm)

    def update(self, member_id: int, alarm_id: int, data: AlarmUpdate) -> AlarmOut:
        alarm = self._get_owned(member_id, alarm_id)
        if data.time is not None:
            alarm.time = data.time
        if data.character_id is not None:
            alarm.character_id = data.character_id
        if data.is_activate is not None:
            alarm.is_activate = data.is_activate
        if data.days_of_week is not None:
            # 기존 요일 통째 교체: clear() → delete-orphan 이 옛 schedule 삭제
            alarm.schedules.clear()
            alarm.schedules.extend(Schedule(day_of_week=d) for d in data.days_of_week)
        self.db.commit()
        self.db.refresh(alarm)
        return self._to_out(alarm)

    def set_active(self, member_id: int, alarm_id: int, active: bool) -> AlarmOut:
        alarm = self._get_owned(member_id, alarm_id)
        alarm.is_activate = active
        self.db.commit()
        self.db.refresh(alarm)
        return self._to_out(alarm)

    def delete(self, member_id: int, alarm_id: int) -> None:
        alarm = self._get_owned(member_id, alarm_id)
        self.repo.delete(alarm)  # schedule 은 FK CASCADE + orphan 으로 함께 삭제
        self.db.commit()

    # ── 내부 ──
    def _get_owned(self, member_id: int, alarm_id: int) -> Alarm:
        alarm = self.repo.get(alarm_id)
        # 존재하지 않거나 내 알람이 아니면 404(존재 노출 방지)
        if alarm is None or alarm.member_id != member_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "알람을 찾을 수 없습니다.")
        return alarm

    def _to_out(self, alarm: Alarm) -> AlarmOut:
        return AlarmOut(
            alarm_id=alarm.alarm_id,
            time=alarm.time,
            is_activate=alarm.is_activate,
            character=AlarmCharacterBrief(
                character_id=alarm.character.character_id,
                name=alarm.character.name,
                image_url=alarm.character.image_url,
            ),
            days_of_week=[s.day_of_week for s in alarm.schedules],
        )
