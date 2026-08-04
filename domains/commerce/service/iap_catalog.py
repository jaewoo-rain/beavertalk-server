"""상품 ID ↔ 우리 도메인(캐릭터·구독) 매핑.

스토어에 등록하는 상품 ID 문자열과 우리 DB 를 잇는 유일한 지점이다.
계약: docs/20260731_1230_IAP-API-계약서-프론트공유용.md §1
재편: docs/20260804_2353_구독-3티어-재편-구현계획.md

⚠ 스토어 상품 ID 는 **한 번 등록하면 영원히 못 바꾼다.** 그래서 여기 문자열은
   표시용 이름이나 DB PK 같은 "바뀔 수 있는 값"에 기대면 안 된다.
   - 캐릭터는 character.product_key(불변 슬러그)로 식별한다. 이름은 마케팅상 바뀔 수
     있고, character_id 는 dev/prod 가 다르다(prod 2·9·10·11 / dev 2·3·4·5) —
     둘 다 영구 식별자로 못 쓴다.
   - 구독은 plan × 주기 4종을 상수로 못 박는다.

⚠ 구 스킴(im.beavertalk.*) 호환은 두지 않는다. 결제 미연동이라 스토어에 등록된 적이
   없어 실제 영수증이 존재하지 않는다. 호환을 남기면 두 스킴이 영구히 공존한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domains.commerce.models.character import Character

PRODUCT_PREFIX_CHARACTER = "bt_character_"

Plan = Literal["pro", "max"]
BillingPeriod = Literal["monthly", "yearly"]

# 구독 상품 4종 → (plan, 주기). 앱(IapProductIds)과 **같은 문자열**이어야 한다.
SUBSCRIPTION_PRODUCTS: dict[str, tuple[str, str]] = {
    "bt_pro_monthly": ("pro", "monthly"),
    "bt_pro_yearly": ("pro", "yearly"),
    "bt_max_monthly": ("max", "monthly"),
    "bt_max_yearly": ("max", "yearly"),
}

# 주기별 폴백 기간(일). 실제 만료는 **스토어 영수증의 expiresDate 가 우선**이고,
# 이 값은 스텁 검증(자격증명 없이 QA)에서만 쓰인다.
PERIOD_DAYS: dict[str, int] = {"monthly": 30, "yearly": 365}


@dataclass(frozen=True)
class ProductRef:
    kind: Literal["character", "subscription"]
    character_id: Optional[int] = None
    plan: Optional[str] = None            # 구독일 때만: pro | max
    billing_period: Optional[str] = None  # 구독일 때만: monthly | yearly


def resolve(db: Session, product_id: str) -> Optional[ProductRef]:
    """상품 ID → ProductRef. 모르는 상품이면 None(호출부가 404).

    캐릭터는 접미사를 character.product_key 와 **대소문자 무시**로 맞춘다
    (스토어엔 소문자로 등록하지만, 백필 값이 섞여 들어올 여지를 남겨 둔다).
    """
    pid = (product_id or "").strip()
    if not pid:
        return None

    sub = SUBSCRIPTION_PRODUCTS.get(pid)
    if sub is not None:
        plan, period = sub
        return ProductRef(kind="subscription", plan=plan, billing_period=period)

    if pid.startswith(PRODUCT_PREFIX_CHARACTER):
        key = pid[len(PRODUCT_PREFIX_CHARACTER):]
        if not key:
            return None
        cid = db.scalar(
            select(Character.character_id).where(
                func.lower(Character.product_key) == key.lower()
            )
        )
        return ProductRef(kind="character", character_id=cid) if cid else None

    return None


def period_days(billing_period: Optional[str]) -> int:
    """주기 → 폴백 기간(일). 모르는 주기는 월간으로 본다(스텁 전용 경로)."""
    return PERIOD_DAYS.get(billing_period or "", PERIOD_DAYS["monthly"])


def product_id_for_character(product_key: str) -> str:
    """캐릭터 슬러그 → 상품 ID(스토어 등록·문서용)."""
    return f"{PRODUCT_PREFIX_CHARACTER}{product_key.strip().lower()}"


def product_id_for_subscription(plan: str, billing_period: str) -> str:
    """plan+주기 → 상품 ID. 정의되지 않은 조합이면 KeyError(등록 실수를 조용히 넘기지 않는다)."""
    for pid, (p, period) in SUBSCRIPTION_PRODUCTS.items():
        if p == plan and period == billing_period:
            return pid
    raise KeyError(f"정의되지 않은 구독 상품: plan={plan} period={billing_period}")
