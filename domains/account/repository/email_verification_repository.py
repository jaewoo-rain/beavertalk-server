"""EmailVerificationRepository — 인증 코드 행 DB 접근 (commit 은 service 책임)."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from domains.account.models.email_verification import EmailVerification


class EmailVerificationRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, email: str, purpose: str) -> Optional[EmailVerification]:
        stmt = select(EmailVerification).where(
            EmailVerification.email == email,
            EmailVerification.purpose == purpose,
        )
        return self.db.scalar(stmt)

    def add(self, row: EmailVerification) -> EmailVerification:
        self.db.add(row)
        return row

    def delete(self, row: EmailVerification) -> None:
        self.db.delete(row)
