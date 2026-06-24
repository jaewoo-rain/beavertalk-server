"""auth 라우터 — 회원가입 / 로그인 (= Spring AuthController).

라우터는 얇게: 요청 검증(DTO) → service 호출 → 응답. 비즈니스 로직 없음.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import EmailStr

from core.deps import DbSession
from core.security import create_access_token
from domains.account.models.email_verification import PURPOSE_SIGNUP
from domains.account.schemas.member import (
    EmailAvailable,
    EmailSendCode,
    EmailVerifyCode,
    MemberCreate,
    MemberRead,
    PasswordResetConfirm,
    PasswordResetRequest,
    SocialLoginRequest,
    Token,
)
from domains.account.service.email_verification_service import (
    EmailVerificationService,
)
from domains.account.service.member_service import MemberService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/email/available", response_model=EmailAvailable)
def email_available(email: EmailStr, db: DbSession) -> EmailAvailable:
    """이메일 사용 가능 여부(중복확인). 가입 폼에서 입력 중 확인용."""
    return EmailAvailable(available=not MemberService(db).email_taken(email))


@router.post("/email/send-code")
def send_signup_code(data: EmailSendCode, db: DbSession) -> dict[str, str]:
    """회원가입 이메일 인증 코드 발송. 이미 가입된 이메일이면 409."""
    EmailVerificationService(db).send_signup_code(data.email)
    return {"message": "인증 코드를 이메일로 보냈습니다."}


@router.post("/email/verify-code")
def verify_signup_code(data: EmailVerifyCode, db: DbSession) -> dict[str, str]:
    """회원가입 이메일 인증 코드 확인. 성공해야 가입 가능."""
    EmailVerificationService(db).verify_code(data.email, PURPOSE_SIGNUP, data.code)
    return {"message": "이메일이 인증되었습니다."}


@router.post("/signup", response_model=MemberRead, status_code=status.HTTP_201_CREATED)
def signup(data: MemberCreate, db: DbSession) -> MemberRead:
    """이메일 회원가입. (speak_country_id·character_id 는 사전 존재 전제 — FK)"""
    return MemberService(db).create(data)


@router.post("/login", response_model=Token)
def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: DbSession,
) -> Token:
    """OAuth2 password 흐름. username 필드에 이메일을 넣는다.

    Swagger UI 의 Authorize 버튼이 이 엔드포인트를 사용한다.
    """
    member = MemberService(db).authenticate(form.username, form.password)
    token = create_access_token(member.member_id)
    return Token(access_token=token)


@router.post("/social", response_model=Token)
def social_login(data: SocialLoginRequest, db: DbSession) -> Token:
    """소셜 로그인. provider 토큰 검증 → 회원 find-or-create → JWT 발급."""
    member = MemberService(db).social_login(data.login_method, data.token)
    return Token(access_token=create_access_token(member.member_id))


@router.post("/password-reset/request")
def password_reset_request(data: PasswordResetRequest, db: DbSession) -> dict[str, str]:
    """재설정 메일 발송 요청. 이메일 존재 여부와 무관하게 동일 응답(추측 방지)."""
    MemberService(db).request_password_reset(data.email)
    return {"message": "재설정 안내를 이메일로 보냈습니다(해당 계정이 있는 경우)."}


@router.post("/password-reset/confirm")
def password_reset_confirm(data: PasswordResetConfirm, db: DbSession) -> dict[str, str]:
    """메일로 받은 4자리 코드 + 새 비밀번호로 비밀번호 변경."""
    MemberService(db).confirm_password_reset(data.email, data.code, data.new_password)
    return {"message": "비밀번호가 변경되었습니다."}
