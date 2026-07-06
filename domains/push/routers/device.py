"""device 라우터 — 푸시 토큰 등록/삭제(로그인 회원 본인)."""

from __future__ import annotations

from fastapi import APIRouter, status

from core.deps import CurrentMember, DbSession
from domains.push.schemas.device import DeviceOut, DeviceRegisterIn
from domains.push.service.device_service import DeviceService

router = APIRouter(prefix="/devices", tags=["devices"])


@router.post("", response_model=DeviceOut, status_code=status.HTTP_201_CREATED)
def register_device(
    data: DeviceRegisterIn, member: CurrentMember, db: DbSession
) -> DeviceOut:
    """푸시 토큰 등록/갱신(같은 토큰이면 소유 회원을 최신으로 upsert)."""
    return DeviceService(db).upsert(member.member_id, data)


@router.delete("/{token}", status_code=status.HTTP_204_NO_CONTENT)
def delete_device(token: str, member: CurrentMember, db: DbSession) -> None:
    """내 푸시 토큰 삭제(로그아웃 등). 없어도 204(멱등)."""
    DeviceService(db).delete(member.member_id, token)
