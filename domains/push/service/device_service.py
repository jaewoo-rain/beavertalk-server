"""DeviceService — 푸시 토큰 등록(upsert)/삭제.

토큰은 전역 UNIQUE 라, 같은 토큰이 기기 초기화·재로그인으로 다른 회원에게
넘어갈 수 있다. 그래서 등록은 Postgres ON CONFLICT(token) upsert 로 원자 처리해
소유 회원을 최신 회원으로 갱신한다(경쟁 조건 없이).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from domains.push.models.device_token import DeviceToken
from domains.push.schemas.device import DeviceOut, DeviceRegisterIn


class DeviceService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def upsert(self, member_id: int, data: DeviceRegisterIn) -> DeviceOut:
        """토큰 등록/갱신(원자 upsert). 같은 토큰이면 소유 회원·유효 상태를 갱신."""
        stmt = (
            pg_insert(DeviceToken)
            .values(
                member_id=member_id,
                platform=data.platform,
                token=data.token,
                is_valid=True,
            )
            .on_conflict_do_update(
                index_elements=[DeviceToken.token],
                set_=dict(
                    member_id=member_id,
                    platform=data.platform,
                    is_valid=True,
                    updated_at=func.now(),
                ),
            )
            .returning(DeviceToken.device_token_id, DeviceToken.platform)
        )
        row = self.db.execute(stmt).one()
        self.db.commit()
        return DeviceOut(device_token_id=row.device_token_id, platform=row.platform)

    def delete(self, member_id: int, token: str) -> None:
        """내 토큰 삭제(로그아웃 등). 없으면 조용히 무시(멱등)."""
        device = self.db.execute(
            select(DeviceToken).where(
                DeviceToken.token == token,
                DeviceToken.member_id == member_id,
            )
        ).scalar_one_or_none()
        if device is not None:
            self.db.delete(device)
            self.db.commit()
