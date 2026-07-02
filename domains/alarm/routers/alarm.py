"""alarm 라우터 — 알람 CRUD + 활성/비활성."""

from __future__ import annotations

from fastapi import APIRouter, status

from core.deps import CurrentMember, DbSession
from domains.alarm.schemas.alarm import AlarmCreate, AlarmOut, AlarmUpdate
from domains.alarm.service.alarm_service import AlarmService

router = APIRouter(prefix="/alarms", tags=["alarms"])


@router.get("", response_model=list[AlarmOut])
def list_alarms(member: CurrentMember, db: DbSession) -> list[AlarmOut]:
    """내 알람 전체 목록(반복 요일 포함)."""
    return AlarmService(db).list(member.member_id)


@router.post("", response_model=AlarmOut, status_code=status.HTTP_201_CREATED)
def create_alarm(data: AlarmCreate, member: CurrentMember, db: DbSession) -> AlarmOut:
    """알람 생성 — 시간·캐릭터·반복 요일을 저장한다."""
    return AlarmService(db).create(member.member_id, data)


@router.get("/{alarm_id}", response_model=AlarmOut)
def get_alarm(alarm_id: int, member: CurrentMember, db: DbSession) -> AlarmOut:
    """알람 단건 조회(내 알람만, 타인/없는 알람이면 404)."""
    return AlarmService(db).get(member.member_id, alarm_id)


@router.put("/{alarm_id}", response_model=AlarmOut)
def update_alarm(
    alarm_id: int, data: AlarmUpdate, member: CurrentMember, db: DbSession
) -> AlarmOut:
    """알람 전체 수정 — 시간·캐릭터·반복 요일을 교체한다."""
    return AlarmService(db).update(member.member_id, alarm_id, data)


@router.delete("/{alarm_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_alarm(alarm_id: int, member: CurrentMember, db: DbSession) -> None:
    """알람 삭제(반복 요일도 CASCADE 로 함께 삭제)."""
    AlarmService(db).delete(member.member_id, alarm_id)


@router.post("/{alarm_id}/activate", response_model=AlarmOut)
def activate_alarm(alarm_id: int, member: CurrentMember, db: DbSession) -> AlarmOut:
    """알람 켜기(활성화)."""
    return AlarmService(db).set_active(member.member_id, alarm_id, True)


@router.post("/{alarm_id}/deactivate", response_model=AlarmOut)
def deactivate_alarm(alarm_id: int, member: CurrentMember, db: DbSession) -> AlarmOut:
    """알람 끄기(비활성화)."""
    return AlarmService(db).set_active(member.member_id, alarm_id, False)
