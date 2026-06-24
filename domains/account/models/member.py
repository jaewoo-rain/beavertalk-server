"""member (회원) — account 도메인."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Identity,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.account.models.member_reason import MemberReason
    from domains.account.models.speak_country import SpeakCountry
    from domains.alarm.models.alarm import Alarm
    from domains.commerce.models.character import Character
    from domains.commerce.models.member_character import MemberCharacter
    from domains.commerce.models.payment import Payment
    from domains.commerce.models.subscribe import Subscribe
    from domains.learning.models.call import Call


class Member(Base, TimestampMixin):
    __tablename__ = "member"
    __table_args__ = (
        # 소셜 계정의 실제 식별 키. (email/password 계정은 둘 다 NULL → 충돌 안 함)
        UniqueConstraint("login_method", "unique_value", name="uq_member_social"),
    )

    member_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    # 억양 / 대표 캐릭터 — 둘 다 선택(nullable). 가입은 email/password 만으로 가능.
    speak_country_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("speak_country.speak_country_id", ondelete="SET NULL"),
        index=True, comment="억양",
    )
    character_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("character.character_id", ondelete="SET NULL"),
        index=True, comment="대표 캐릭터",
    )

    name: Mapped[Optional[str]] = mapped_column(Text, comment="이름(온보딩에서 입력)")
    language: Mapped[Optional[str]] = mapped_column(Text, comment="사용 언어")
    login_method: Mapped[Optional[str]] = mapped_column(Text, comment="로그인 방법")
    unique_value: Mapped[Optional[str]] = mapped_column(Text, index=True, comment="소셜 유니크 번호")
    email: Mapped[Optional[str]] = mapped_column(Text, unique=True, comment="이메일(소셜은 없을 수 있음)")
    password: Mapped[Optional[str]] = mapped_column(Text, comment="비밀번호(해시 저장 권장)")
    is_auto_payment: Mapped[Optional[bool]] = mapped_column(Boolean, comment="정기구독 여부")
    onboarding_completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
        comment="온보딩(이름·학습이유·언어) 완료 여부",
    )

    # ── 부모(M:1) — 모두 lazy. 필요한 화면에서 쿼리에 joinedload 로 명시 fetch ──
    speak_country: Mapped[Optional["SpeakCountry"]] = relationship(
        back_populates="members", lazy="select",
    )
    representative_character: Mapped[Optional["Character"]] = relationship(
        foreign_keys=[character_id], lazy="select",
    )

    # ── 자식(1:N) ──
    reasons: Mapped[list["MemberReason"]] = relationship(
        back_populates="member", cascade="all, delete-orphan",
        passive_deletes=True, lazy="selectin",
    )
    alarms: Mapped[list["Alarm"]] = relationship(
        back_populates="member", cascade="all, delete-orphan", passive_deletes=True,
    )
    subscribes: Mapped[list["Subscribe"]] = relationship(
        back_populates="member", cascade="all, delete-orphan", passive_deletes=True,
    )
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="member", cascade="all, delete-orphan", passive_deletes=True,
    )
    calls: Mapped[list["Call"]] = relationship(
        back_populates="member", cascade="all, delete-orphan", passive_deletes=True,
    )
    owned_characters: Mapped[list["MemberCharacter"]] = relationship(
        back_populates="member", cascade="all, delete-orphan",
        passive_deletes=True, lazy="selectin",
    )
