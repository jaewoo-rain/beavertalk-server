"""character (캐릭터/페르소나) — commerce 도메인. 마스터 데이터.

캐릭터 = 통화 상대 페르소나. 역할(role)·성격(personality)으로
프롬프트를 구성하고, 실시간 통화 음성은 voice(Gemini Live 프리빌트 보이스)를 참조한다.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import TYPE_CHECKING, Optional
from uuid import uuid4

from sqlalchemy import JSON, BigInteger, ForeignKey, Identity, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.commerce.models.discount_event import DiscountEvent
    from domains.commerce.models.member_character import MemberCharacter
    from domains.commerce.models.voice import Voice


def _derive_product_key(context) -> str:
    """INSERT 시 name → 슬러그. 마이그레이션 백필과 같은 규칙(영숫자만·소문자).

    이름이 비었거나 특수문자뿐이면 난수 슬러그로 떨어뜨린다 — NOT NULL 을 못 채워
    INSERT 가 죽는 것보다 낫고, UNIQUE 충돌도 피한다.
    """
    params = context.get_current_parameters() or {}
    slug = re.sub(r"[^a-z0-9]", "", str(params.get("name") or "").lower())
    return slug[:32] or f"c{uuid4().hex[:8]}"


class Character(Base, TimestampMixin):
    __tablename__ = "character"

    character_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    voice_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("voice.voice_id", ondelete="SET NULL"),
        index=True, comment="실시간 통화 음성(Gemini Live voice)",
    )
    role: Mapped[Optional[str]] = mapped_column(Text, comment="역할/정체성")
    personality: Mapped[Optional[str]] = mapped_column(Text, comment="성격·말투·톤")
    voice_url: Mapped[Optional[str]] = mapped_column(Text, comment="캐릭터 프리뷰 샘플 음성 URL")
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), comment="가격(달러)")
    name: Mapped[str] = mapped_column(Text, comment="캐릭터 이름")
    # ⭐ 스토어 상품 ID 용 불변 슬러그(bt_character_{product_key}).
    #
    # 왜 name 도 character_id 도 아닌 제3의 값인가:
    #   - name: 캐릭터 이름은 마케팅 자산이라 바뀔 수 있는데, **스토어 상품 ID 는 한 번
    #     등록하면 영원히 못 바꾼다**. 이름을 쓰면 개명하는 순간 어긋난다.
    #   - character_id: dev 와 prod 의 id 가 다르다(prod 2·9·10·11 / dev 2·3·4·5).
    #     dev 에서 산 캐릭터가 prod 에선 다른 캐릭터가 된다. 내부 PK 가 영구 공개
    #     식별자로 새는 것도 좋지 않다.
    # 그래서 표시 이름과 PK 양쪽에서 분리한 슬러그를 둔다. 초기값은 lower(name) 백필이라
    # 지금 당장의 동작은 같고, 앞으로 이름만 자유롭게 바꿀 수 있다.
    # 미지정이면 name 에서 자동 파생한다(마이그레이션 백필과 같은 규칙). 캐릭터를
    # 만들 때마다 슬러그를 손으로 정하게 하면 빠뜨리거나 오타가 난다. 한 번 정해지면
    # 이름을 바꿔도 따라 바뀌지 않는다 — 그게 이 컬럼의 존재 이유다.
    product_key: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, default=_derive_product_key,
        comment="스토어 상품 ID 슬러그(불변)",
    )
    description: Mapped[Optional[str]] = mapped_column(Text, comment="세부 설명")
    story: Mapped[Optional[str]] = mapped_column(Text, comment="캐릭터 스토리/서사(배경 이야기)")
    gender: Mapped[Optional[str]] = mapped_column(Text, comment="캐릭터 성별 느낌(male/female)")
    image_url: Mapped[Optional[str]] = mapped_column(Text, comment="캐릭터 이미지")
    tags: Mapped[Optional[list[str]]] = mapped_column(
        JSON, comment="음색/특성 태그 배열(예: Warm, Calm, Soft)"
    )

    voice: Mapped[Optional["Voice"]] = relationship(
        back_populates="characters", lazy="select",
    )
    owners: Mapped[list["MemberCharacter"]] = relationship(
        back_populates="character", lazy="select",
    )
    discount_events: Mapped[list["DiscountEvent"]] = relationship(
        back_populates="character", cascade="all, delete-orphan",
        passive_deletes=True, lazy="select",
    )
