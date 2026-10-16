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
    # 모국어(ISO 639-1 · 2026-10-12). 없고 language 만 오면 서버가 language 로 채운다(빌드 58 이하 호환).
    native_language: Optional[str] = None
    # 실제 국적(ISO 3166-1 alpha-2 · 2026-10-10). 국가 표 밖이면 422. 없으면 그대로 둔다.
    actual_nationality: Optional[str] = None


class ConsentItemIn(BaseModel):
    """동의 항목 하나 — 항목 코드와 화면이 보여준 문서 버전."""

    item: str
    version: str


class ConsentIn(BaseModel):
    """가입 동의 기록(PM-DEC-735·736) — 필수 4항목을 한 번에 보낸다."""

    items: list[ConsentItemIn]
    app_version: Optional[str] = None
    platform: Optional[str] = None
    locale: Optional[str] = None


class MemberUpdate(BaseModel):
    """회원 수정 입력(부분 수정). 모두 선택값."""

    language: Optional[str] = None  # 모국어. "ko-KR" 같은 BCP-47 도 서버가 "ko" 로 정규화
    # 모국어 재선택(2026-10-12). null·빈 값은 「변경 없음」. language 를 바꿔도 이 칸은 그대로다.
    native_language: Optional[str] = None
    target_language: Optional[str] = None  # 학습 대상 언어(ISO 639-1) — 마이페이지 피커
    # 실제 국적(ISO 3166-1 alpha-2) — 마이페이지 국적 화면 Save. null 은 「변경 없음」(앱에서 지우는 길 없음 · PM-DEC-502).
    actual_nationality: Optional[str] = None
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
    target_language: Optional[str] = None  # 학습 대상 언어 — 마이페이지가 이 값을 표시한다
    actual_nationality: Optional[str] = None  # 실제 국적(ISO) — 설정 Account 「Nationality」 행
    native_language: Optional[str] = None  # 온보딩에서 고른 모국어(ISO 639-1) · NULL=기록 없음
    is_subscribed: bool
    onboarding_completed: bool
    speak_country: Optional[SpeakCountryOut]
    reasons: list[str] = []
    # 종합 레벨 카드(마이페이지). **target_language 스코프**다 — 회원은 언어마다 레벨이
    # 따로라 member.korean_level 을 그대로 쓰면 안 된다(ko 가 아니면 남의 레벨을 보여준다).
    # None = 레벨테스트 미실시 → 앱은 "레벨테스트 하기"로 안내한다.
    korean_level: Optional[int] = None
    level_max: int = 13
    # 레벨별 고정 표에서 나온 "상위 N%" (실제 분포 계산 아님 — level_percentile 참고).
    # 표에 없는 레벨이거나 레벨 미확정이면 None → 앱이 그 줄을 숨긴다.
    level_top_percent: Optional[int] = None


# ── 응답 DTO ──
class MemberRead(BaseModel):
    """회원 응답. password 제외."""

    model_config = ConfigDict(from_attributes=True)  # ORM 객체 → DTO 자동 변환

    member_id: int
    email: Optional[str]
    name: Optional[str]
    language: Optional[str]
    target_language: Optional[str] = None  # 학습 대상 언어(통화 target_language 의 단일 소스)
    actual_nationality: Optional[str] = None  # 실제 국적(ISO 3166-1 alpha-2) · NULL=미선택
    native_language: Optional[str] = None  # 온보딩에서 고른 모국어(ISO 639-1) · NULL=기록 없음
    is_auto_payment: Optional[bool]
    speak_country_id: Optional[int]
    character_id: Optional[int]
    korean_level: Optional[int] = None  # 한국어 레벨(1~13). None=레벨테스트 미실시
    onboarding_completed: bool
    reasons: list[str] = []
    # 가입 동의(PM-DEC-735·736) — True 면 앱이 홈·온보딩 전에 동의 화면을 띄운다. 라우터가 채운다(ORM 칸 아님).
    consent_required: bool = False
    # 동의 화면이 보낼 현행 문서 버전(항목 코드 → 버전). 앱은 이 값을 그대로 POST /members/me/consents 에 싣는다.
    consent_versions: dict[str, str] = {}
    created_at: datetime
    updated_at: datetime

    @field_validator("reasons", mode="before")
    @classmethod
    def _reason_codes(cls, v: object) -> list[str]:
        """ORM 의 list[MemberReason] → list[str](코드)로 변환."""
        if not v:
            return []
        return [getattr(r, "reason", r) for r in v]
