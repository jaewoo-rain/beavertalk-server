"""payment (결제) — commerce 도메인. member 와 N:1."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.account.models.member import Member


class Payment(Base, TimestampMixin):
    __tablename__ = "payment"

    payment_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # ⛔⛔ S2(2026-09-26, Play 심사 대비) — nullable + ON DELETE SET NULL(옛 CASCADE).
    #   결제 기록은 법무 보존 의무 대상이라 회원 하드 삭제에 같이 지워지면 안 된다.
    #   같은 커밋의 마이그레이션(c2d4e6f8a0b1)과 반드시 같은 내용이어야 한다(R2) —
    #   sqlite(테스트)는 이 파일의 제약을 쓰고 운영은 그 마이그레이션을 쓴다.
    member_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("member.member_id", ondelete="SET NULL"), index=True, comment="회원",
    )
    payment_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="결제 날짜")
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), comment="결제 금액")
    description: Mapped[Optional[str]] = mapped_column(Text, comment="결제 내용")
    category: Mapped[Optional[str]] = mapped_column(
        Text, index=True, comment="결제 분류(subscribe/character)"
    )
    card_info: Mapped[Optional[str]] = mapped_column(Text, comment="카드 정보(마스킹)")

    # ⛔ Optional — SET NULL 대상이라 탈퇴 회원의 결제 행은 이 관계가 None 이 된다.
    member: Mapped[Optional["Member"]] = relationship(back_populates="payments")
