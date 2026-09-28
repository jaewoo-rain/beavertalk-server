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
from domains.learning.service import mastery_service
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


@router.post("/me/level-test/retake")
def retake_level_test(member: CurrentMember, db: DbSession) -> dict:
    """레벨테스트 다시 받기 — 「재측정 대기」만 표시한다(**체크판·학습 기록은 보존**).

    마이페이지 "레벨테스트 다시하기" 버튼의 통로. 성공하면 다음 통화가 자동으로
    레벨테스트로 라우팅된다(D11: 레벨 미확정 또는 재측정 대기 → level_test).

    ⛔⛔ §9(2026-09-28) — 이 호출은 **레벨을 즉시 지우지 않는다.** 옛 동작(즉시
    삭제)은 재측정 성공과 통화 성립 사이(연결 중 취소·ALREADY_IN_CALL) 실패하면
    레벨만 지워진 채 남는 문제가 있었다. 실제 초기화(member_language_level 행
    삭제 + ko 면 member.korean_level NULL)는 그 레벨테스트 통화가 실제로 **성립한
    뒤**(call_started, call_session.py)에 일어난다 — 통화가 안 되면 옛 레벨이
    그대로 남는다.

    - 남기는 것: member_item_progress(체크판)·item_evidence(증거)·승급 이력·통화 기록·
      (통화가 성립하기 전까지는) 옛 레벨 자체

    ⚠ 하루 1회 제한은 여기서 막지 않는다. 이 호출은 "다음 통화를 레벨테스트로"
    표시만 하고, 실제 거절은 통화 시작 시점(call_session)이 call_type='level_test'
    한도로 한다. 판정을 한 곳에 두는 편이 두 곳에서 각자 세다 어긋나는 것보다 낫다.
    즉 오늘 이미 레벨테스트를 했다면 이 호출은 200 이지만 통화가 DAILY_LIMIT 로
    거절된다 — 앱은 통화 진입에서 그 에러를 처리해야 한다.

    ⭐ 「재측정 대기 취소」기능은 없다(앱에 취소 버튼 없음, YAGNI). 이 호출 뒤
    통화를 영영 안 하면 다음 통화도 계속 level_test 로 라우팅된다 — 이건 의도다.
    사용자가 원할 때 레벨테스트를 받으면 되고, 그 전까지는 옛 레벨로 정상 학습한다.

    dev 전용 `POST /__dev/level-reset` 과 혼동하지 말 것 — 그건 체크판·증거까지
    전부 지우는 완전 백지화(관리자용)다.
    """
    return mastery_service.request_level_retest(
        db, member, member.target_language or "ko"
    )


@router.patch("/me", response_model=MemberRead)
def update_me(data: MemberUpdate, member: CurrentMember, db: DbSession) -> MemberRead:
    return MemberService(db).update(member.member_id, data)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_me(member: CurrentMember, db: DbSession) -> None:
    MemberService(db).delete(member.member_id)
