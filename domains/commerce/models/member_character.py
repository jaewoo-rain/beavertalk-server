"""member_character (구매한 캐릭터) — commerce 도메인.

member×character 다대다 Association Object. 복합 PK (member_id, character_id) 가
'같은 캐릭터 중복구매 불가'(uk_one_character) 를 보장.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from domains.account.models.member import Member
    from domains.commerce.models.character import Character


class MemberCharacter(Base):
    __tablename__ = "member_character"

    member_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("member.member_id", ondelete="CASCADE"), primary_key=True,
        comment="구매한 사람",
    )
    character_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("character.character_id", ondelete="RESTRICT"), primary_key=True,
        index=True, comment="구매한 캐릭터",
    )
    purchase_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 2), comment="실제 구매한 가격",
    )
    purchase_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="구매한 날짜",
    )

    member: Mapped["Member"] = relationship(back_populates="owned_characters")
    character: Mapped["Character"] = relationship(back_populates="owners")
