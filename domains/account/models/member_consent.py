"""member_consent (회원 가입 동의 기록) — account 도메인.

앱 온보딩 첫 화면(동의 화면)에서 받은 필수 동의를 **항목별 한 행**으로 남긴다(PM-DEC-735·736).
- 추가 전용(append-only): 문서 버전이 바뀌어 다시 동의하면 새 행이 쌓인다. 고치거나 지우지 않는다.
- 「지금 동의가 유효한가」는 항목마다 현행 버전(`consent_service.CURRENT_VERSIONS`) 행이 있는지로 본다.
- 회원 하드 삭제(탈퇴) 때 함께 지워진다(ON DELETE CASCADE · member_service.delete 와 같은 규칙).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class MemberConsent(Base):
    __tablename__ = "member_consent"
    __table_args__ = (
        Index("ix_member_consent_member_item", "member_id", "item"),
    )

    member_consent_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE"), nullable=False
    )
    item: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="동의 항목 코드(age14·terms·privacy_collect·overseas_transfer)",
    )
    doc_version: Mapped[str] = mapped_column(
        Text, nullable=False, comment="동의한 문서 버전(시행일 YYYY-MM-DD 등)"
    )
    agreed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
        comment="동의 시각(서버 기준)",
    )
    app_version: Mapped[Optional[str]] = mapped_column(Text, comment="동의한 앱 버전(예 1.0.0+72)")
    platform: Mapped[Optional[str]] = mapped_column(Text, comment="android·ios 등")
    locale: Mapped[Optional[str]] = mapped_column(Text, comment="동의 화면 UI 언어(BCP-47)")
