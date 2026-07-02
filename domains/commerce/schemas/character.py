"""character 관련 DTO."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict


class DiscountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    discount_price: Optional[Decimal]
    start_time: Optional[datetime]
    end_time: Optional[datetime]


class CharacterSummary(BaseModel):
    """목록용 — 카드에 필요한 값 일괄 제공. prompt(내부용) 제외.

    목록 화면 카드가 설명·미리듣기 음성까지 한 번(GET /characters)에 그리도록
    description/voice_url 을 포함한다(캐릭터당 상세조회 N+1 회피).
    """

    character_id: int
    name: str
    image_url: Optional[str]
    description: Optional[str]  # 카드 설명
    voice_url: Optional[str]   # 미리듣기 샘플 음성 URL
    tags: list[str] = []       # 음색/특성 태그(칩) — 없으면 빈 배열
    price: Decimal
    effective_price: Decimal  # 활성 할인 반영가(서버 계산)
    is_owned: bool            # 현재 회원 소유 여부


class CharacterDetail(CharacterSummary):
    """상세용 — 요약 필드 + 활성 할인 정보."""

    active_discount: Optional[DiscountOut]


class OwnedCharacterOut(BaseModel):
    """내 소유 캐릭터 1건(구매 정보 평탄화)."""

    character_id: int
    name: str
    image_url: Optional[str]
    description: Optional[str]  # 시트 설명(보유 캐릭터도 표시)
    voice_url: Optional[str]   # 미리듣기 샘플 음성 URL
    tags: list[str] = []  # 음색/특성 태그(칩)
    purchase_price: Optional[Decimal]
    purchase_date: Optional[datetime]
