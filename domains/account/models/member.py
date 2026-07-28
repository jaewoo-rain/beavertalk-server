"""member (회원) — account 도메인."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    Text,
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
    from domains.push.models.device_token import DeviceToken


class Member(Base, TimestampMixin):
    __tablename__ = "member"

    member_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    # Supabase Auth(auth.users) 연결 키. 인증·비번·소셜은 Supabase 가 관리하고,
    # 우리 member 는 이 UUID 로 매핑한다(앱 내부 식별은 member_id 유지).
    auth_user_id: Mapped[Optional[str]] = mapped_column(
        Text, unique=True, index=True, comment="Supabase auth.users.id (UUID)"
    )

    # 억양 / 대표 캐릭터 — 둘 다 선택(nullable).
    speak_country_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("speak_country.speak_country_id", ondelete="SET NULL"),
        index=True, comment="억양",
    )
    character_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("character.character_id", ondelete="SET NULL"),
        index=True, comment="대표 캐릭터",
    )

    name: Mapped[Optional[str]] = mapped_column(Text, comment="이름(온보딩에서 입력)")
    language: Mapped[Optional[str]] = mapped_column(Text, comment="모국어(번역 target locale)")
    # (멀티랭귀지) 현재 학습 대상 언어 — 통화 target_language 의 단일 소스.
    # 옛날엔 앱 SharedPreferences 가 원본이라 ① 하이드레이션 레이스(복원 전에 통화가 시작되면
    # 저장값 대신 기본 'ko' 전송 — 잠금화면 수신통화가 그 구간) ② 앱 재설치 시 리셋 ③ 서버가
    # 거는 예약전화인데 언어는 클라가 정하는 모순이 있었다. 이제 서버가 소유한다.
    # "어떤 언어들을 배우는가"는 member_language_level(회원×언어 1행)이 이미 안다 —
    # 이 컬럼은 "그중 지금 활성인 것" 하나만 가리킨다.
    # 근거: docs/20260728_0125_학습언어-DB-단일소스화와-모국어-정규화.md
    target_language: Mapped[Optional[str]] = mapped_column(
        Text, server_default=text("'ko'"),
        comment="학습 대상 언어(ISO 639-1). NULL=기본 ko",
    )
    email: Mapped[Optional[str]] = mapped_column(Text, unique=True, comment="이메일(Supabase 에서 동기화)")
    is_auto_payment: Mapped[Optional[bool]] = mapped_column(Boolean, comment="정기구독 여부")
    onboarding_completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
        comment="온보딩(이름·학습이유·언어) 완료 여부",
    )

    # 소프트 삭제(회원 탈퇴). 값이 있으면 탈퇴한 회원 — 학습·구독 등 데이터는 보존하고
    # 재식별 키(email·auth_user_id)만 NULL 로 끊어 같은 이메일 재가입을 허용한다.
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), index=True, comment="탈퇴 시각(소프트 삭제, NULL=활성)",
    )

    # ── normalcall 학습 프로파일 (통화 프롬프트 주입용) ──
    # 흥미는 member_reason(온보딩 학습이유)에서 가져온다(별도 interests 컬럼 제거).
    korean_level: Mapped[Optional[int]] = mapped_column(
        Integer, comment="한국어 레벨(1~13 → level.level_no, 1=생존 회화)",
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
    device_tokens: Mapped[list["DeviceToken"]] = relationship(
        back_populates="member", cascade="all, delete-orphan", passive_deletes=True,
    )
