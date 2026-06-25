"""member 관련 Pydantic 스키마(DTO).

Spring 의 Request/Response DTO 에 해당. ORM 엔티티를 그대로 노출하지 않고
여기서 입력 검증·출력 형태를 정의한다. password 는 응답에 절대 포함하지 않는다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator


# ── 요청 DTO ──
class OnboardingIn(BaseModel):
    """온보딩 입력 — 이름 + 학습이유 + 언어. (가입 직후 별도 단계)"""

    name: Optional[str] = None
    reasons: Optional[list[str]] = None  # 학습 이유 코드(다중선택)
    language: Optional[str] = None


class MemberUpdate(BaseModel):
    """회원 수정 입력(부분 수정). 모두 선택값."""

    language: Optional[str] = None
    character_id: Optional[int] = None
    is_auto_payment: Optional[bool] = None


class SpeakCountryOut(BaseModel):
    """억양 전체 필드."""

    model_config = ConfigDict(from_attributes=True)

    speak_country_id: int
    first_country: Optional[str]
    second_country: Optional[str]
    third_country: Optional[str]
    first_percent: Optional[int]
    second_percent: Optional[int]
    third_percent: Optional[int]


class MyPageOut(BaseModel):
    """마이페이지 — 억양 전체 + 사용 언어 + 구독 여부 + 학습 이유."""

    member_id: int
    email: Optional[str]
    name: Optional[str]
    language: Optional[str]
    is_subscribed: bool
    onboarding_completed: bool
    speak_country: Optional[SpeakCountryOut]
    reasons: list[str] = []


# ── 응답 DTO ──
class MemberRead(BaseModel):
    """회원 응답. password 제외."""

    model_config = ConfigDict(from_attributes=True)  # ORM 객체 → DTO 자동 변환

    member_id: int
    email: Optional[str]
    name: Optional[str]
    language: Optional[str]
    is_auto_payment: Optional[bool]
    speak_country_id: Optional[int]
    character_id: Optional[int]
    onboarding_completed: bool
    reasons: list[str] = []
    created_at: datetime
    updated_at: datetime

    @field_validator("reasons", mode="before")
    @classmethod
    def _reason_codes(cls, v: object) -> list[str]:
        """ORM 의 list[MemberReason] → list[str](코드)로 변환."""
        if not v:
            return []
        return [getattr(r, "reason", r) for r in v]
