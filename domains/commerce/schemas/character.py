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
    # 스토어 상품 ID 슬러그(bt_character_{product_key}). 앱이 결제 시트를 띄울 때
    # 이 값으로 상품 ID 를 만든다 — character_id 는 dev/prod 가 다르고(prod 2·9·10·11 /
    # dev 2·3·4·5), name 은 바뀔 수 있는데 스토어 상품 ID 는 영구 불변이라 둘 다 못 쓴다.
    product_key: str
    name: str
    image_url: Optional[str]
    description: Optional[str]  # 카드 설명
    background_story: Optional[str] = None  # 캐릭터 배경 이야기/서사(목록·상세 공통 — 카드 미리보기용)
    voice_url: Optional[str]   # 미리듣기 샘플 음성 URL
    tags: list[str] = []       # 음색/특성 태그(칩) — 없으면 빈 배열
    price: Decimal
    effective_price: Decimal  # 활성 할인 반영가(서버 계산)
    is_owned: bool            # 영구 소유 여부(member_character 행) — 돈 주고 산 것
    # 지금 **쓸 수 있는가**. 소유했거나, 구독(Max)이 열어줬거나.
    # ⛔ is_owned 와 섞지 않는다. Max 가 여는 건 접근이지 소유가 아니라서 해지하면
    #   닫힌다 — 앱의 downgradeWarning("Max-only characters turn off on {date}")이
    #   그 사실을 이미 화면에서 말한다. 하나로 합치면 "샀다"고 오해시킨 뒤 뺏는 꼴이 된다.
    # 구버전 앱은 이 필드를 모르고 is_owned 만 본다 → 종전 동작(잠김) 유지 = 안전한 폴백.
    is_unlocked: bool = False
    # 무엇이 열어줬나 — "owned"(구매) | "subscription"(Max) | None(잠김).
    # 앱이 "구매" CTA 와 "Max 혜택" 배지를 구분해 그릴 수 있게 이유를 실어 보낸다.
    unlock_source: Optional[str] = None
    # 한정 할인 카운트다운용 — end_time 이 마감 시각이다. 상세 화면은 목록에서 카드를 눌러
    # 진입하고 추가 조회를 하지 않으므로(N+1 회피), 카운트다운을 그리려면 종료 시각이
    # **목록 응답에** 있어야 한다. 없으면(활성 할인 부재) None.
    active_discount: Optional[DiscountOut] = None


class CharacterDetail(CharacterSummary):
    """상세용 — 요약 필드 + 성별.

    active_discount 는 CharacterSummary 로 승격돼 상속받는다(출력 형태 불변).
    """

    gender: Optional[str] = None  # 캐릭터 성별 느낌(male/female, 상세 전용)


class OwnedCharacterOut(BaseModel):
    """내 소유 캐릭터 1건(구매 정보 평탄화)."""

    character_id: int
    name: str
    image_url: Optional[str]
    description: Optional[str]  # 시트 설명(보유 캐릭터도 표시)
    background_story: Optional[str] = None  # 캐릭터 배경 이야기/서사
    voice_url: Optional[str]   # 미리듣기 샘플 음성 URL
    tags: list[str] = []  # 음색/특성 태그(칩)
    purchase_price: Optional[Decimal]
    purchase_date: Optional[datetime]
