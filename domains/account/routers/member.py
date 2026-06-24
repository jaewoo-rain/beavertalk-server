"""member 라우터 — 내 프로필 조회/수정/탈퇴 (= Spring MemberController).

모든 엔드포인트가 CurrentMember 로 인증을 요구한다(= @AuthenticationPrincipal).
"""

from __future__ import annotations

from fastapi import APIRouter, status

from core.deps import CurrentMember, DbSession
from domains.account.schemas.member import (
    MemberRead,
    MemberUpdate,
    MyPageOut,
    OnboardingIn,
)
from domains.account.service.member_service import MemberService

router = APIRouter(prefix="/members", tags=["members"])


@router.get("/me", response_model=MemberRead)
def get_me(member: CurrentMember) -> MemberRead:
    """현재 로그인한 회원 정보. 토큰에서 주입된 member 를 그대로 반환."""
    return member


@router.post("/me/onboarding", response_model=MemberRead)
def onboarding(data: OnboardingIn, member: CurrentMember, db: DbSession) -> MemberRead:
    """온보딩 — 이름·학습이유·언어 저장(회원가입 직후 별도 단계)."""
    return MemberService(db).onboarding(
        member.member_id, data.name, data.reasons, data.language
    )


@router.get("/me/profile", response_model=MyPageOut)
def get_my_page(member: CurrentMember, db: DbSession) -> MyPageOut:
    """마이페이지 — 억양 전체 + 사용 언어 + 구독 여부."""
    return MemberService(db).get_my_page(member.member_id)


@router.patch("/me", response_model=MemberRead)
def update_me(data: MemberUpdate, member: CurrentMember, db: DbSession) -> MemberRead:
    return MemberService(db).update(member.member_id, data)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_me(member: CurrentMember, db: DbSession) -> None:
    MemberService(db).delete(member.member_id)
