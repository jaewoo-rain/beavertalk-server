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

from core.supabase_auth import delete_auth_user
from domains.account.models.member import Member
from domains.account.models.member_reason import ALLOWED_REASONS, MemberReason
from domains.account.repository.member_repository import MemberRepository
from domains.account.schemas.member import (
    MemberUpdate,
    MyPageOut,
    SpeakCountryOut,
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

    def find_or_create_by_auth(
        self, auth_user_id: str, email: Optional[str]
    ) -> Member:
        """Supabase auth user(uuid)로 member 를 찾고, 없으면 자동 프로비저닝.

        인증은 Supabase 가 끝낸 뒤(토큰 검증 완료) 호출된다. 신규면 기본(첫 무료)
        캐릭터를 지정·보유시키고 onboarding_completed=False 로 만든다.
        """
        member = self.repo.get_by_auth(auth_user_id)
        if member is not None:
            if email and member.email != email:  # 이메일 변경 동기화
                member.email = email
                self.db.commit()
            return member

        character_id, owned = self._resolve_default_character(None)
        member = Member(
            auth_user_id=auth_user_id,
            email=email,
            character_id=character_id,
            owned_characters=owned,
        )
        self.repo.add(member)
        self.db.commit()
        self.db.refresh(member)
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

    def update(self, member_id: int, data: MemberUpdate) -> Member:
        member = self.get(member_id)
        # 전달된 필드만 부분 수정
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(member, field, value)
        self.db.commit()
        self.db.refresh(member)
        return member

    def delete(self, member_id: int) -> None:
        """회원 탈퇴 — Supabase auth 사용자 + 로컬 member 를 함께 삭제.

        인증 주체(auth.users)를 먼저 지운다. 로컬 member 만 지우면 남은 access token
        으로 다음 요청이 오는 순간 find_or_create_by_auth 가 member 를 재생성해 계정이
        부활한다. auth 삭제가 실패하면(미설정·네트워크·권한) 탈퇴 자체를 실패로 처리해
        로컬만 지워지는(=부활 가능한) 불일치 상태를 막는다.
        """
        member = self.get(member_id)
        if member.auth_user_id and not delete_auth_user(member.auth_user_id):
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "인증 서버에서 계정을 삭제하지 못했습니다. 잠시 후 다시 시도해주세요.",
            )
        self.repo.delete(member)
        self.db.commit()
