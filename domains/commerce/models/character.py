"""character (캐릭터) — commerce 도메인. 마스터 데이터."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, Identity, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.commerce.models.discount_event import DiscountEvent
    from domains.commerce.models.member_character import MemberCharacter


class Character(Base, TimestampMixin):
    __tablename__ = "character"

    character_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    prompt: Mapped[Optional[str]] = mapped_column(Text, comment="생성용 프롬프트")
    voice_url: Mapped[Optional[str]] = mapped_column(Text, comment="캐릭터 목소리 URL")
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), comment="가격(달러)")
    name: Mapped[str] = mapped_column(Text, comment="캐릭터 이름")
    description: Mapped[Optional[str]] = mapped_column(Text, comment="세부 설명")
    image_url: Mapped[Optional[str]] = mapped_column(Text, comment="캐릭터 이미지")

    owners: Mapped[list["MemberCharacter"]] = relationship(
        back_populates="character", lazy="select",
    )
    discount_events: Mapped[list["DiscountEvent"]] = relationship(
        back_populates="character", cascade="all, delete-orphan",
        passive_deletes=True, lazy="select",
    )
