"""email_verification (이메일 인증 코드) — account 도메인.

비밀번호 재설정(`purpose="pwreset"`)에서 쓰는 4자리 코드 저장소.
코드는 bcrypt 해시로 보관하고, (email, purpose) 당 1행만 유지한다
(재발송 시 갱신). 시도 횟수·만료·인증완료 시각을 함께 관리한다.

JWT 토큰이 아니라 **서버 DB**에 두는 이유: JWT 본문은 디코드가 가능해 4자리(1만 개)는
오프라인 무차별 대입에 뚫린다. 서버 저장 + 시도제한 + 만료로 보호한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Identity, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin

# purpose 값
PURPOSE_PWRESET = "pwreset"


class EmailVerification(Base, TimestampMixin):
    __tablename__ = "email_verification"
    __table_args__ = (
        # 이메일+용도 당 1행만(재발송은 갱신).
        UniqueConstraint("email", "purpose", name="uq_email_verification"),
    )

    email_verification_id: Mapped[int] = mapped_column(
        BigInteger, Identity(), primary_key=True
    )
    email: Mapped[str] = mapped_column(Text, index=True, comment="대상 이메일")
    purpose: Mapped[str] = mapped_column(Text, comment="pwreset")
    code_hash: Mapped[str] = mapped_column(Text, comment="코드 bcrypt 해시")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), comment="코드 만료 시각"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", comment="코드 입력 시도 횟수"
    )
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="인증 완료 시각"
    )
