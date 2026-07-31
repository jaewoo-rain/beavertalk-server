"""상품 ID ↔ 우리 도메인(캐릭터·구독) 매핑.

스토어에 등록하는 상품 ID 문자열과 우리 DB 를 잇는 유일한 지점이다.
계약: docs/20260731_1230_IAP-API-계약서-프론트공유용.md §1

⚠ character_id 를 하드코딩하지 않는다 — dev 와 prod 의 캐릭터 id 가 다르다
   (prod: 2·9·10·11 / dev: 2·3·4·5). 이름으로 조회해야 환경이 바뀌어도 안 깨진다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domains.commerce.models.character import Character

PRODUCT_PREFIX_CHARACTER = "im.beavertalk.character."
PRODUCT_SUBSCRIPTION_MONTHLY = "im.beavertalk.pro.monthly"

# 구독 1건이 주는 기간(일). 실제 만료는 스토어 영수증의 expiresDate 가 우선이고,
# 이 값은 스텁·폴백용이다(월간 1종 — 결정 사항).
SUBSCRIPTION_PERIOD_DAYS = 30


@dataclass(frozen=True)
class ProductRef:
    kind: Literal["character", "subscription"]
    character_id: Optional[int] = None


def resolve(db: Session, product_id: str) -> Optional[ProductRef]:
    """상품 ID → ProductRef. 모르는 상품이면 None(호출부가 404).

    캐릭터는 접미사(bibi/popo/…)를 character.name 과 **대소문자 무시**로 맞춘다.
    DB 표기가 환경마다 'BIBI'/'Bibi' 로 섞여 있어서다.
    """
    pid = (product_id or "").strip()
    if not pid:
        return None

    if pid == PRODUCT_SUBSCRIPTION_MONTHLY:
        return ProductRef(kind="subscription")

    if pid.startswith(PRODUCT_PREFIX_CHARACTER):
        name = pid[len(PRODUCT_PREFIX_CHARACTER):]
        if not name:
            return None
        cid = db.scalar(
            select(Character.character_id).where(
                func.lower(Character.name) == name.lower()
            )
        )
        return ProductRef(kind="character", character_id=cid) if cid else None

    return None


def product_id_for_character(name: str) -> str:
    """캐릭터 이름 → 상품 ID(스토어 등록·문서용)."""
    return f"{PRODUCT_PREFIX_CHARACTER}{name.strip().lower()}"
