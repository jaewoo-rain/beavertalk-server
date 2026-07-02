"""character (캐릭터/페르소나) — commerce 도메인. 마스터 데이터.

캐릭터 = 통화 상대 페르소나. 역할(role)·성격(personality)·추가규칙(rules)으로
프롬프트를 구성하고, 실시간 통화 음성은 voice(Gemini Live 프리빌트 보이스)를 참조한다.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import JSON, BigInteger, ForeignKey, Identity, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.commerce.models.discount_event import DiscountEvent
    from domains.commerce.models.member_character import MemberCharacter
    from domains.commerce.models.voice import Voice


class Character(Base, TimestampMixin):
    __tablename__ = "character"

    character_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    voice_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("voice.voice_id", ondelete="SET NULL"),
        index=True, comment="실시간 통화 음성(Gemini Live voice)",
    )
    role: Mapped[Optional[str]] = mapped_column(Text, comment="역할/정체성")
    personality: Mapped[Optional[str]] = mapped_column(Text, comment="성격·말투·톤")
    rules: Mapped[Optional[str]] = mapped_column(Text, comment="캐릭터별 추가 규칙/금기")
    voice_url: Mapped[Optional[str]] = mapped_column(Text, comment="캐릭터 프리뷰 샘플 음성 URL")
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), comment="가격(달러)")
    name: Mapped[str] = mapped_column(Text, comment="캐릭터 이름")
    description: Mapped[Optional[str]] = mapped_column(Text, comment="세부 설명")
    image_url: Mapped[Optional[str]] = mapped_column(Text, comment="캐릭터 이미지")
    tags: Mapped[Optional[list[str]]] = mapped_column(
        JSON, comment="음색/특성 태그 배열(예: Warm, Calm, Soft)"
    )

    voice: Mapped[Optional["Voice"]] = relationship(
        back_populates="characters", lazy="select",
    )
    owners: Mapped[list["MemberCharacter"]] = relationship(
        back_populates="character", lazy="select",
    )
    discount_events: Mapped[list["DiscountEvent"]] = relationship(
        back_populates="character", cascade="all, delete-orphan",
        passive_deletes=True, lazy="select",
    )
