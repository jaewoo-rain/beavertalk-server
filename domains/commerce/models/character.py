"""character (캐릭터/페르소나) — commerce 도메인. 마스터 데이터.

캐릭터 = 통화 상대 페르소나. 역할(role)·성격(personality)으로
프롬프트를 구성하고, 실시간 통화 음성은 voice(Gemini Live 프리빌트 보이스)를 참조한다.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import TYPE_CHECKING, Optional
from uuid import uuid4

from sqlalchemy import JSON, BigInteger, Boolean, ForeignKey, Identity, Numeric, String, Text
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
    # ⭐⭐ §2(2026-09-28, 사장님 확정) — 캐릭터 묶음(bt_character_bundle) 구성의
    #   **유일한 정본**. Play Developer API 실측(monetization.oneTimeProducts):
    #   OneTimeProduct 스키마엔 자식 상품 필드가 없다(productId·listings·
    #   purchaseOptions·offerTags·taxAndComplianceSettings·restrictedPaymentCountries·
    #   regionsVersion 뿐) — 내용물은 영어 설명 한 줄("Unlock Popo, Rara and Dudu,
    #   yours forever")로만 존재한다. 마케팅 문구를 파싱해 구성을 읽는 건 문구
    #   수정·다국어화에 바로 깨진다. 애플도 IAP 에 같은 개념이 없다.
    #   ⇒ 스토어에서 구조적으로 읽어올 방법이 없으므로 우리 쪽에 정본을 둔다.
    #   ⛔ "price>0 전부"로 파생하지 않는다 — 캐릭터가 계속 추가되는데 신규 유료
    #   캐릭터가 자동으로 묶음에 들어가면(=과거 판매 내용과 다른 걸 새로 파는 셈)
    #   사장님 요구와 어긋난다. 기본값 False 라 새 캐릭터는 묶음에 자동으로 안
    #   들어간다 — 넣고 싶으면 이 컬럼을 UPDATE 한 줄로 켠다(배포 불필요).
    #   ⚠ 이 컬럼을 바꾸면 Play Console 의 그 설명 문구도 사람이 같이 고쳐야 한다
    #   (스토어와 여기는 서로 다른 진실이고 동기화가 자동이 아니다).
    #   ⛔ "스토어에서 읽어오게 고쳐라"로 되돌리지 마라 — 위 실측이 이미 불가능을
    #   확인했다.
    in_bundle: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
        comment="캐릭터 묶음(bt_character_bundle)에 포함되는가 — 스토어엔 내용물 "
                "필드가 없어 여기가 유일한 정본",
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
