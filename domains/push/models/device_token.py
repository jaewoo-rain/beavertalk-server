"""device_token (푸시 토큰) — push 도메인. member 와 1:N.

예약전화 FCM 발송 대상. platform 별로 토큰을 보관하고, 발송 실패(폐기)로
확인된 토큰은 is_valid=False 로 끈다(soft invalidate). member 탈퇴 시 CASCADE.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, ForeignKey, Identity, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.account.models.member import Member


class DeviceToken(Base, TimestampMixin):
    __tablename__ = "device_token"

    device_token_id: Mapped[int] = mapped_column(
        BigInteger, Identity(), primary_key=True
    )
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE"), index=True, comment="회원",
    )
    platform: Mapped[str] = mapped_column(Text, comment="android_fcm | ios_voip")
    token: Mapped[str] = mapped_column(Text, unique=True, index=True, comment="푸시 토큰")
    is_valid: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"),
        comment="유효 여부(발송 폐기 시 False)",
    )

    member: Mapped["Member"] = relationship(back_populates="device_tokens")
