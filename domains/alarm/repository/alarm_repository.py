"""AlarmRepository — 알람 조회/추가/삭제. schedule·character 는 함께 eager 로드."""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from domains.alarm.models.alarm import Alarm


class AlarmRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    @staticmethod
    def _load_opts() -> list:
        # 컬렉션(schedules)=selectinload, 스칼라(character)=joinedload → N+1 방지.
        # 메서드 안에서 생성(호출 시점)해야 모든 모델이 등록된 뒤 관계가 해석됨.
        return [selectinload(Alarm.schedules), joinedload(Alarm.character)]

    def get(self, alarm_id: int) -> Optional[Alarm]:
        return self.db.get(Alarm, alarm_id, options=self._load_opts())

    def list_by_member(self, member_id: int) -> Sequence[Alarm]:
        stmt = (
            select(Alarm)
            .where(Alarm.member_id == member_id)
            .options(*self._load_opts())
            .order_by(Alarm.alarm_id)
        )
        return self.db.scalars(stmt).all()

    def add(self, alarm: Alarm) -> Alarm:
        self.db.add(alarm)
        return alarm

    def delete(self, alarm: Alarm) -> None:
        self.db.delete(alarm)
