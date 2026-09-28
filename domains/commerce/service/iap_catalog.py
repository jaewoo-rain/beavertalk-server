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

# ⭐⭐ 캐릭터 묶음(§2, 2026-09-28 — 앱팀 「비버톡_서버전달_2026-09-28.md」, 출시 전).
#   ⛔ "bt_character_bundle" 은 PRODUCT_PREFIX_CHARACTER 와 문자열이 겹친다 — 아래
#   resolve() 에서 접두사 분기보다 **먼저** 걸러야 한다(안 그러면 product_key==
#   "bundle" 인 캐릭터를 찾다 못 찾고 404, 앱팀이 보고한 그 버그의 정확한 원인).
#
#   ⛔⛔ 정정(2026-09-28, 사장님 지시 — 하드코딩 금지) — 구성을 product_key 상수
#   목록으로 못박지 않는다. 스토어(Play Developer API monetization.oneTimeProducts
#   — 대문자 T 경로를 제대로 찾아 실호출) 실측: OneTimeProduct 스키마 필드는
#   productId·listings·purchaseOptions·offerTags·taxAndComplianceSettings·
#   restrictedPaymentCountries·regionsVersion **뿐이다 — 자식 상품 필드가 없다.**
#   내용물은 영어 마케팅 설명 한 줄("Unlock Popo, Rara and Dudu, yours forever")
#   로만 존재한다 — 문구를 파싱해 구성을 읽는 건 문구 수정·다국어화에 바로
#   깨진다. 애플도 IAP 에 같은 개념이 없다. ⇒ 스토어에서 구조적으로 읽을 방법이
#   없으므로 우리 쪽에 정본을 둔다: character.in_bundle(모델 참조).
#   ⛔ "price>0 전부"도 아니다(한때 그렇게 했다가 되돌렸다) — 캐릭터가 계속
#   추가되는데 신규 유료 캐릭터가 자동으로 묶음에 들어가면 사장님이 판 적 없는
#   구성을 파는 셈이 된다. in_bundle 기본값 False 라 새 캐릭터는 자동으로 묶음에
#   안 들어간다 — 넣고 싶으면 그 컬럼을 UPDATE 한 줄로 켠다(배포 불필요).
#   ⚠ in_bundle 을 바꾸면 Play Console 의 그 설명 문구도 사람이 같이 고쳐야 한다.
#   ⛔ "스토어에서 읽어오게 고쳐라"로 되돌리지 마라 — 위 실측이 이미 불가능을 확인했다.
BUNDLE_PRODUCT_ID = "bt_character_bundle"

Plan = Literal["premium"]
BillingPeriod = Literal["monthly", "yearly"]

# 구독 상품 4종 → (plan, 주기). 앱(IapProductIds)과 **같은 문자열**이어야 한다.
# ⭐ D1(2026-09-22): 옛 pro·max 는 전부 premium 하나다. 상품 ID(bt_pro_*·bt_max_*)는
#   스토어 등록값이라 이름은 그대로 두고 값만 premium 으로 매핑한다 — 스토어 정리 시 id 교체.
SUBSCRIPTION_PRODUCTS: dict[str, tuple[str, str]] = {
    "bt_pro_monthly": ("premium", "monthly"),
    "bt_pro_yearly": ("premium", "yearly"),
    "bt_max_monthly": ("premium", "monthly"),
    "bt_max_yearly": ("premium", "yearly"),
}

# 주기별 폴백 기간(일). 실제 만료는 **스토어 영수증의 expiresDate 가 우선**이고,
# 이 값은 스텁 검증(자격증명 없이 QA)에서만 쓰인다.
PERIOD_DAYS: dict[str, int] = {"monthly": 30, "yearly": 365}


@dataclass(frozen=True)
class ProductRef:
    kind: Literal["character", "subscription", "bundle"]
    character_id: Optional[int] = None
    plan: Optional[str] = None            # 구독일 때만: premium
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

    # ⛔ 묶음 판정은 접두사 분기보다 먼저 — BUNDLE_PRODUCT_ID 정의부 주석 참조.
    if pid == BUNDLE_PRODUCT_ID:
        return ProductRef(kind="bundle")

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


def resolve_bundle_character_ids(db: Session) -> list[int]:
    """묶음 구성 = **구매 시점**에 character.in_bundle=True 인 캐릭터 전부(하드코딩
    없음 — 위 BUNDLE_PRODUCT_ID 주석 참조). 구성을 바꾸고 싶으면 그 컬럼을 UPDATE
    한 줄로 켜거나 끈다 — 배포 불필요.

    ⭐⭐ 소급 지급 걱정은 없다 — verify_and_grant 의 ③ 멱등 체크(기존 영수증
    조회)가 지급보다 **먼저** already_granted 로 반환한다. 비소모성(캐릭터·묶음)
    은 같은 사용자가 같은 상품을 두 번 살 수 없어 새 transaction_id 자체가 생기지
    않는다 — 즉 이 함수가 "지금 뭐가 들었나"를 매번 다시 계산해도, 이미 한 번
    지급받은 회원이 나중에 바뀐 구성으로 재지급/재복원되는 경로가 없다. 다음
    사람이 이걸 "소급 지급 버그"로 오해해 플래그 컬럼·스냅샷을 추가하지 마라 —
    필요 없다.
    """
    rows = db.scalars(
        select(Character.character_id).where(Character.in_bundle.is_(True))
    ).all()
    return list(rows)


def period_days(billing_period: Optional[str]) -> int:
    """주기 → 폴백 기간(일). 모르는 주기는 월간으로 본다(스텁 전용 경로)."""
    return PERIOD_DAYS.get(billing_period or "", PERIOD_DAYS["monthly"])


def product_id_for_character(product_key: str) -> str:
    """캐릭터 슬러그 → 상품 ID(스토어 등록·문서용)."""
    return f"{PRODUCT_PREFIX_CHARACTER}{product_key.strip().lower()}"


def product_id_for_subscription(plan: str, billing_period: str) -> str:
    """plan+주기 → 상품 ID. 정의되지 않은 조합이면 KeyError(등록 실수를 조용히 넘기지 않는다).

    ⚠ D1 이후 premium+주기는 pid 두 개(bt_pro_*·bt_max_*)와 매핑된다 — dict 순서상
      bt_pro_* 가 먼저 잡힌다. 역방향 조회(문서·스텁)용이라 실제 지급 경로엔 안 쓰인다.
    """
    for pid, (p, period) in SUBSCRIPTION_PRODUCTS.items():
        if p == plan and period == billing_period:
            return pid
    raise KeyError(f"정의되지 않은 구독 상품: plan={plan} period={billing_period}")
