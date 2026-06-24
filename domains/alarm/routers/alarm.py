"""alarm 라우터 — 알람 CRUD + 활성/비활성."""

from __future__ import annotations

from fastapi import APIRouter, status

from core.deps import CurrentMember, DbSession
from domains.alarm.schemas.alarm import AlarmCreate, AlarmOut, AlarmUpdate
from domains.alarm.service.alarm_service import AlarmService

router = APIRouter(prefix="/alarms", tags=["alarms"])


@router.get("", response_model=list[AlarmOut])
def list_alarms(member: CurrentMember, db: DbSession) -> list[AlarmOut]:
    return AlarmService(db).list(member.member_id)


@router.post("", response_model=AlarmOut, status_code=status.HTTP_201_CREATED)
def create_alarm(data: AlarmCreate, member: CurrentMember, db: DbSession) -> AlarmOut:
    return AlarmService(db).create(member.member_id, data)


@router.get("/{alarm_id}", response_model=AlarmOut)
def get_alarm(alarm_id: int, member: CurrentMember, db: DbSession) -> AlarmOut:
    return AlarmService(db).get(member.member_id, alarm_id)


@router.put("/{alarm_id}", response_model=AlarmOut)
def update_alarm(
    alarm_id: int, data: AlarmUpdate, member: CurrentMember, db: DbSession
) -> AlarmOut:
    return AlarmService(db).update(member.member_id, alarm_id, data)


@router.delete("/{alarm_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_alarm(alarm_id: int, member: CurrentMember, db: DbSession) -> None:
    AlarmService(db).delete(member.member_id, alarm_id)


@router.post("/{alarm_id}/activate", response_model=AlarmOut)
def activate_alarm(alarm_id: int, member: CurrentMember, db: DbSession) -> AlarmOut:
    return AlarmService(db).set_active(member.member_id, alarm_id, True)


@router.post("/{alarm_id}/deactivate", response_model=AlarmOut)
def deactivate_alarm(alarm_id: int, member: CurrentMember, db: DbSession) -> AlarmOut:
    return AlarmService(db).set_active(member.member_id, alarm_id, False)
