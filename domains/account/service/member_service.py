"""MemberService — 비즈니스 로직 + 트랜잭션 경계.

Spring 의 @Service + @Transactional 에 해당. **여기서 db.commit() 을 명시적으로 호출**한다
(get_db 는 close 만 담당하는 명시적 커밋 전략).
비밀번호 해싱 같은 도메인 규칙도 여기서 처리.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Sequence

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.security import hash_password, verify_password
from core.social import SocialAuthError, verify_social_token
from domains.account.models.email_verification import PURPOSE_PWRESET
from domains.account.models.member import Member
from domains.account.models.member_reason import ALLOWED_REASONS, MemberReason
from domains.account.repository.member_repository import MemberRepository
from domains.account.schemas.member import (
    MemberCreate,
    MemberUpdate,
    MyPageOut,
    SpeakCountryOut,
)
from domains.account.service.email_verification_service import (
    EmailVerificationService,
)
from domains.commerce.models.character import Character
from domains.commerce.models.member_character import MemberCharacter
from domains.commerce.models.subscribe import Subscribe


class MemberService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = MemberRepository(db)

    def get(self, member_id: int) -> Member:
        member = self.repo.get(member_id)
        if member is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "회원을 찾을 수 없습니다.")
        return member

    def list(self, limit: int = 50, offset: int = 0) -> Sequence[Member]:
        return self.repo.list(limit=limit, offset=offset)

    def get_my_page(self, member_id: int) -> MyPageOut:
        """마이페이지 — 억양 전체 + 언어 + 구독 여부(집계)."""
        member = self.repo.get_with_speak_country(member_id)
        if member is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "회원을 찾을 수 없습니다.")
        is_subscribed = self._has_active_subscription(member_id)
        return MyPageOut(
            member_id=member.member_id,
            email=member.email,
            name=member.name,
            language=member.language,
            is_subscribed=is_subscribed,
            onboarding_completed=member.onboarding_completed,
            speak_country=(
                SpeakCountryOut.model_validate(member.speak_country)
                if member.speak_country
                else None
            ),
            reasons=[r.reason for r in member.reasons],
        )

    def _has_active_subscription(self, member_id: int) -> bool:
        stmt = (
            select(Subscribe.subscribe_id)
            .where(Subscribe.member_id == member_id, Subscribe.is_activate.is_(True))
            .limit(1)
        )
        return self.db.scalar(stmt) is not None

    def create(self, data: MemberCreate) -> Member:
        # 이메일 중복 검사 (DB UNIQUE 제약과 별개로 친절한 메시지 제공)
        if self.repo.get_by_email(data.email) is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "이미 가입된 이메일입니다.")

        # 가입은 이메일 + 비밀번호만. 이름·학습이유·언어는 온보딩에서 채운다.
        # 대표 캐릭터는 서버가 기본(첫 무료) 캐릭터로 자동 지정 + 보유 처리.
        character_id, owned = self._resolve_default_character(None)

        member = Member(
            email=data.email,
            password=hash_password(data.password),  # 평문 저장 금지
            character_id=character_id,
            owned_characters=owned,
        )
        self.repo.add(member)
        self.db.commit()       # ← 트랜잭션 커밋 (명시적)
        self.db.refresh(member)  # DB 가 채운 PK·타임스탬프 반영
        return member

    def onboarding(
        self,
        member_id: int,
        name: Optional[str],
        reasons: Optional[list[str]],
        language: Optional[str],
    ) -> Member:
        """온보딩 저장 — 이름·학습이유·언어. 전달된 항목만 반영(reasons 는 교체)."""
        member = self.get(member_id)
        if name is not None:
            member.name = name
        if language is not None:
            member.language = language
        if reasons is not None:
            codes = self._validate_reasons(reasons)
            # 기존 이유 교체(cascade delete-orphan 으로 옛 행 정리).
            member.reasons = [MemberReason(reason=code) for code in codes]
        member.onboarding_completed = True  # 온보딩 단계 완료 표시
        self.db.commit()
        self.db.refresh(member)
        return member

    def _resolve_default_character(
        self, requested: Optional[int]
    ) -> tuple[Optional[int], list[MemberCharacter]]:
        """대표 캐릭터 결정 + 무료 스타터 자동 보유.

        - 대표 캐릭터: 요청값이 있으면 그대로, 없으면 첫(가장 낮은 id) 캐릭터.
        - 첫 캐릭터가 무료(price 0)면 그 캐릭터를 자동으로 보유 처리(스타터 지급).
        캐릭터가 하나도 없으면 (None, []) 반환(가입은 진행됨).
        """
        starter = self.db.scalar(
            select(Character).order_by(Character.character_id).limit(1)
        )
        if starter is None:
            return requested, []

        character_id = requested or starter.character_id
        owned: list[MemberCharacter] = []
        if starter.price == 0:
            owned.append(
                MemberCharacter(
                    character_id=starter.character_id,
                    purchase_price=Decimal("0.00"),
                    purchase_date=datetime.now(timezone.utc),
                )
            )
        return character_id, owned

    @staticmethod
    def _validate_reasons(reasons: Optional[list[str]]) -> list[str]:
        """학습 이유 코드 화이트리스트 검증 + 중복 제거(순서 유지)."""
        if not reasons:
            return []
        seen: list[str] = []
        for code in reasons:
            if code not in ALLOWED_REASONS:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"알 수 없는 학습 이유: {code}"
                )
            if code not in seen:
                seen.append(code)
        return seen

    def email_taken(self, email: str) -> bool:
        """이메일이 이미 가입에 사용됐는지(중복확인 API 용)."""
        return self.repo.get_by_email(email) is not None

    def social_login(self, login_method: str, token: str) -> Member:
        """소셜 토큰 검증 → 기존 회원이면 로그인, 없으면 가입(find-or-create)."""
        try:
            identity = verify_social_token(login_method, token)
        except SocialAuthError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
        member = self.repo.get_by_social(identity.login_method, identity.unique_value)
        if member is not None:
            return member
        member = Member(
            login_method=identity.login_method,
            unique_value=identity.unique_value,
            email=identity.email,   # 없으면 None (소셜은 이메일 없을 수 있음)
            password=None,          # 소셜 전용 계정은 비번 없음
        )
        self.repo.add(member)
        self.db.commit()
        self.db.refresh(member)
        return member

    def request_password_reset(self, email: str) -> None:
        """재설정 4자리 코드 발송. 회원 존재 여부는 노출하지 않는다(항상 성공처럼)."""
        EmailVerificationService(self.db).send_reset_code(email)
        # 회원이 없거나 소셜 전용이어도 동일 응답 → 이메일 존재 여부 추측 방지

    def confirm_password_reset(self, email: str, code: str, new_password: str) -> None:
        """메일로 받은 코드 검증 → 새 비밀번호로 교체 → 인증 행 소비."""
        verifier = EmailVerificationService(self.db)
        verifier.verify_code(email, PURPOSE_PWRESET, code)  # 실패 시 400

        member = self.repo.get_by_email(email)
        if member is None or member.password is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "유효하지 않은 요청입니다.")
        member.password = hash_password(new_password)
        verifier.consume_verified(email, PURPOSE_PWRESET)  # 1회용 소비
        self.db.commit()

    def authenticate(self, email: str, password: str) -> Member:
        """이메일+비밀번호 검증. 실패 시 401 (어느 쪽이 틀렸는지 노출 안 함)."""
        member = self.repo.get_by_email(email)
        if (
            member is None
            or member.password is None
            or not verify_password(password, member.password)
        ):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "이메일 또는 비밀번호가 올바르지 않습니다.",
            )
        return member

    def update(self, member_id: int, data: MemberUpdate) -> Member:
        member = self.get(member_id)
        # 전달된 필드만 부분 수정
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(member, field, value)
        self.db.commit()
        self.db.refresh(member)
        return member

    def delete(self, member_id: int) -> None:
        member = self.get(member_id)
        self.repo.delete(member)
        self.db.commit()
