"""EmailVerificationService — 이메일 인증 코드 발급/검증/소비.

회원가입(signup)·비밀번호 재설정(pwreset)이 공용으로 쓴다.
- 발급: 4자리 코드 생성 → bcrypt 해시 저장(평문 미보관) → 메일 발송. (email, purpose) 당 1행.
- 검증: 만료/시도제한/코드일치 확인. 성공 시 verified_at 기록(틀리면 attempts++).
- 소비: 인증 완료(verified) 행을 삭제(=1회용). 회원가입 완료 시 호출.

코드 생성([generate_code])은 모듈 함수로 분리 — 테스트에서 monkeypatch 해 결정적으로 만든다.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from core.config import settings
from core.email import send_email
from core.security import hash_password, verify_password
from domains.account.models.email_verification import (
    PURPOSE_PWRESET,
    PURPOSE_SIGNUP,
    EmailVerification,
)
from domains.account.repository.email_verification_repository import (
    EmailVerificationRepository,
)
from domains.account.repository.member_repository import MemberRepository


def generate_code() -> str:
    """`EMAIL_CODE_LENGTH` 자리 0-패딩 숫자 코드(암호학적 난수)."""
    n = settings.EMAIL_CODE_LENGTH
    return str(secrets.randbelow(10**n)).zfill(n)


def _aware(dt: datetime) -> datetime:
    """naive datetime(=sqlite)을 UTC aware 로 보정(Postgres는 이미 aware)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class EmailVerificationService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = EmailVerificationRepository(db)
        self.member_repo = MemberRepository(db)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    # ── 발급 ──
    def send_signup_code(self, email: str) -> None:
        """회원가입용 코드 발송. 이미 가입된 이메일이면 409."""
        if self.member_repo.get_by_email(email) is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "이미 가입된 이메일입니다.")
        self._issue(email, PURPOSE_SIGNUP, "[BeaverTalk] 이메일 인증 코드")

    def send_reset_code(self, email: str) -> None:
        """비밀번호 재설정용 코드 발송(존재 여부 비노출 — 항상 조용히 처리).

        가입된 이메일(비번 계정)일 때만 실제 발송하고, 레이트리밋 등 예외는 삼킨다.
        """
        member = self.member_repo.get_by_email(email)
        if member is None or member.password is None:
            return
        try:
            self._issue(email, PURPOSE_PWRESET, "[BeaverTalk] 비밀번호 재설정 코드")
        except HTTPException:
            pass  # 레이트리밋도 동일 응답(추측 방지)

    def _issue(self, email: str, purpose: str, subject: str) -> None:
        """코드 생성·저장·발송. (email, purpose) 행을 갱신(없으면 생성)."""
        existing = self.repo.get(email, purpose)
        if existing is not None:
            issued_at = _aware(existing.expires_at) - timedelta(
                minutes=settings.EMAIL_CODE_EXPIRE_MINUTES
            )
            if (self._now() - issued_at).total_seconds() < settings.EMAIL_CODE_RESEND_SECONDS:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "잠시 후 다시 시도해주세요.",
                )

        code = generate_code()
        expires_at = self._now() + timedelta(
            minutes=settings.EMAIL_CODE_EXPIRE_MINUTES
        )
        if existing is not None:
            existing.code_hash = hash_password(code)
            existing.expires_at = expires_at
            existing.attempts = 0
            existing.verified_at = None
        else:
            self.repo.add(
                EmailVerification(
                    email=email,
                    purpose=purpose,
                    code_hash=hash_password(code),
                    expires_at=expires_at,
                )
            )
        self.db.commit()

        send_email(
            to=email,
            subject=subject,
            body=(
                f"인증 코드: {code}\n"
                f"{settings.EMAIL_CODE_EXPIRE_MINUTES}분 안에 입력해주세요."
            ),
        )

    # ── 검증 ──
    def verify_code(self, email: str, purpose: str, code: str) -> None:
        """코드 검증 → 성공 시 verified_at 기록. 실패 시 400(틀리면 attempts++)."""
        row = self.repo.get(email, purpose)
        if row is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "인증 요청을 먼저 진행해주세요.")
        if self._now() > _aware(row.expires_at):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "인증 코드가 만료되었습니다.")
        if row.attempts >= settings.EMAIL_CODE_MAX_ATTEMPTS:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "시도 횟수를 초과했습니다. 코드를 다시 요청해주세요.",
            )
        if not verify_password(code, row.code_hash):
            row.attempts += 1
            self.db.commit()
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "인증 코드가 올바르지 않습니다.")
        row.verified_at = self._now()
        self.db.commit()

    # ── 소비 ──
    def consume_verified(self, email: str, purpose: str) -> bool:
        """인증 완료(verified·미만료) 행이 있으면 삭제하고 True. (commit 은 호출자)"""
        row = self.repo.get(email, purpose)
        if row is None or row.verified_at is None or self._now() > _aware(row.expires_at):
            return False
        self.repo.delete(row)
        self.db.flush()
        return True
