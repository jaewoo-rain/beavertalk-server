"""IapService — 영수증 검증 → 멱등 확인 → 지급 → 권한 반환.

🧒 전체 흐름(이 순서가 중요하다):
    ① 상품 ID 를 우리 도메인으로 해석    (모르면 404 — 스토어에 없는 상품)
    ② 스토어에 영수증 검증               (무효 422 / 스토어 불통 503)
    ③ 이미 처리한 거래인가?              (맞으면 재지급 없이 성공 — 멱등)
    ④ 지급 + 영수증 기록을 **한 트랜잭션**으로
    ⑤ 최신 권한(entitlement) 반환        (앱이 이걸로 화면 갱신)

②를 ③보다 먼저 두는 이유: 위조 영수증이 "이미 처리됨"을 노려 조회만 유발하는 걸 막고,
검증 없이 DB 를 뒤지지 않기 위해서다.

계약: docs/20260731_1230_IAP-API-계약서-프론트공유용.md
설명: docs/20260731_1200_결제-처음-보는-사람을-위한-안내.md
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core import iap
from domains.commerce.models.iap_receipt import IapReceipt
from domains.commerce.models.member_character import MemberCharacter
from domains.commerce.models.subscribe import Subscribe
from domains.commerce.schemas.iap import (
    Entitlement,
    PurchaseItem,
    RestoreResponse,
    VerifyResponse,
)
from domains.commerce.service import iap_catalog

logger = logging.getLogger(__name__)


class IapService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ── 조회 ────────────────────────────────────────────────────────────── #
    def entitlement(self, member_id: int) -> Entitlement:
        """이 회원이 **지금** 가진 권한. "Pro 인가"의 단일 진실.

        만료 판정을 서버가 한다 — 앱이 만료 시각을 자체 비교하면 기기 시계 조작·시차로
        어긋난다. 앱은 is_pro 를 그대로 쓴다.
        """
        now = datetime.now(timezone.utc)
        sub = self.db.scalar(
            select(Subscribe)
            .where(Subscribe.member_id == member_id, Subscribe.is_activate.is_(True))
            .order_by(Subscribe.end_date.desc().nullslast())
        )
        expires = sub.end_date if sub else None
        # end_date 가 없으면(무기한) 활성으로 본다. 있으면 지금과 비교.
        is_pro = bool(sub) and (expires is None or _as_utc(expires) > now)
        # ⛔ on_hold(결제 유예도 끝남)는 **접근 차단**, grace(재시도 중)는 **접근 유지**.
        #   이 비대칭이 두 상태를 나눈 이유 전부다. 앱도 같은 규칙으로 짜여 있어서
        #   (subscription_state.dart — grantsPaidAccess) 여기가 어긋나면
        #   "앱은 되는데 서버가 거절"이 된다.
        if sub is not None and sub.billing_state == "on_hold":
            is_pro = False

        owned = list(
            self.db.scalars(
                select(MemberCharacter.character_id).where(
                    MemberCharacter.member_id == member_id
                )
            )
        )
        return Entitlement(
            is_pro=is_pro,
            pro_expires_at=expires if is_pro else None,
            owned_character_ids=sorted(owned),
        )

    # ── 검증 + 지급 ─────────────────────────────────────────────────────── #
    def verify_and_grant(
        self,
        member_id: int,
        platform: str,
        item: PurchaseItem,
        is_sandbox: bool = False,
    ) -> VerifyResponse:
        # ① 상품 해석
        ref = iap_catalog.resolve(self.db, item.product_id)
        if ref is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "UNKNOWN_PRODUCT",
                    "message": "알 수 없는 상품입니다.",
                },
            )

        # ② 스토어 검증 (앱 말을 믿지 않는 지점)
        result = iap.verify(
            platform=platform,  # type: ignore[arg-type]
            product_id=item.product_id,
            transaction_id=item.transaction_id,
            purchase_token=item.purchase_token,
            is_sandbox=is_sandbox,
        )
        if not result.ok:
            if result.reason == "unavailable":
                # 스토어가 응답을 안 준 것 — 영수증 잘못이 아니다. 앱은 재시도해도 된다.
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "code": "VERIFY_UNAVAILABLE",
                        "message": "결제 확인이 지연되고 있어요. 잠시 후 다시 시도해 주세요.",
                    },
                )
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "INVALID_RECEIPT",
                    "message": "결제 정보를 확인할 수 없어요.",
                },
            )

        tx_id = result.transaction_id or item.transaction_id

        # ③ 멱등 — 이미 처리한 거래면 재지급 없이 성공
        existing = self.db.scalar(
            select(IapReceipt).where(
                IapReceipt.platform == platform,
                IapReceipt.transaction_id == tx_id,
            )
        )
        if existing is not None:
            if existing.member_id != member_id:
                # 다른 계정이 쓴 영수증(가족 공유·계정 전환). 지급하면 안 된다.
                logger.warning(
                    "iap: 영수증 소유자 불일치 tx=%s owner=%s requester=%s",
                    tx_id, existing.member_id, member_id,
                )
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail={
                        "code": "RECEIPT_OWNED_BY_OTHER",
                        "message": "다른 계정에서 사용된 결제입니다.",
                    },
                )
            return VerifyResponse(
                already_granted=True,
                product_id=existing.product_id,
                kind=existing.kind,  # type: ignore[arg-type]
                character_id=existing.character_id,
                entitlement=self.entitlement(member_id),
            )

        # ④ 지급 + 기록 (한 트랜잭션)
        expires_at = None
        if ref.kind == "character":
            self._grant_character(member_id, ref.character_id)  # type: ignore[arg-type]
        else:
            expires_at = self._grant_subscription(
                member_id, result.expires_at, ref, item.product_id
            )

        self.db.add(IapReceipt(
            member_id=member_id,
            platform=platform,
            transaction_id=tx_id,
            product_id=item.product_id,
            kind=ref.kind,
            character_id=ref.character_id,
            expires_at=expires_at,
            is_sandbox=is_sandbox,
            is_stub=result.stubbed,
        ))
        try:
            self.db.commit()
        except IntegrityError:
            # 동시에 같은 영수증이 두 번 들어온 경우 — UNIQUE 가 잡았다.
            # 상대 트랜잭션이 지급을 끝냈으므로 성공으로 돌린다(멱등).
            self.db.rollback()
            logger.info("iap: 동시 요청 경합 → 기존 지급으로 수렴 tx=%s", tx_id)
            return VerifyResponse(
                already_granted=True,
                product_id=item.product_id,
                kind=ref.kind,  # type: ignore[arg-type]
                character_id=ref.character_id,
                entitlement=self.entitlement(member_id),
            )

        logger.info(
            "iap: 지급 완료 member=%s product=%s kind=%s stub=%s",
            member_id, item.product_id, ref.kind, result.stubbed,
        )
        return VerifyResponse(
            already_granted=False,
            product_id=item.product_id,
            kind=ref.kind,  # type: ignore[arg-type]
            character_id=ref.character_id,
            entitlement=self.entitlement(member_id),
        )

    def restore(
        self,
        member_id: int,
        platform: str,
        purchases: list[PurchaseItem],
        is_sandbox: bool = False,
    ) -> RestoreResponse:
        """과거 영수증 일괄 복원. 일부가 무효여도 200 — 유효한 것만 지급한다.

        🧒 왜 필요한가: 폰을 바꾸거나 앱을 지웠다 깔면 산 캐릭터가 사라진다. 스토어엔
          구매 기록이 남아 있으므로 앱이 그걸 꺼내 보내면 서버가 소유권을 되살린다.
          캐릭터를 **영구 소유**로 정했으므로 필수 기능이다.
        """
        restored = failed = 0
        for p in purchases:
            try:
                res = self.verify_and_grant(member_id, platform, p, is_sandbox)
                # already_granted 도 복원 성공이다(이미 갖고 있다는 뜻).
                restored += 1 if not res.already_granted else 0
            except HTTPException as exc:
                failed += 1
                logger.info(
                    "iap(restore): 건너뜀 product=%s status=%s", p.product_id, exc.status_code
                )
        return RestoreResponse(
            restored=restored, failed=failed, entitlement=self.entitlement(member_id)
        )

    # ── 지급 ────────────────────────────────────────────────────────────── #
    def _grant_character(self, member_id: int, character_id: int) -> None:
        """소유권 부여. 이미 있으면 조용히 통과(복원 경로)."""
        exists = self.db.get(MemberCharacter, (member_id, character_id))
        if exists is not None:
            return
        self.db.add(MemberCharacter(
            member_id=member_id,
            character_id=character_id,
            purchase_price=None,  # 가격은 스토어가 정한다 — 서버가 모른다
            purchase_date=datetime.now(timezone.utc),
        ))

    def _grant_subscription(
        self,
        member_id: int,
        store_expires_at: object | None,
        ref: iap_catalog.ProductRef,
        product_id: str,
    ) -> datetime:
        """구독 활성화. 만료는 **스토어 값이 우선**, 없으면 주기별 폴백(스텁용).

        기존 활성 구독이 있으면 만료를 연장하고 **플랜·주기도 갱신**한다 — Pro→Max
        업그레이드나 월납→연납 전환이 같은 경로로 들어오는데, 만료만 늘리면 회원은
        Max 를 샀는데 서버는 Pro 로 남는다.

        source='store': 결제 미연동 기간에 만든 행(manual)과 구분하는 표식이다.
        이게 없으면 결제가 붙는 날 "누가 진짜 유료인가"를 못 가른다.
        """
        now = datetime.now(timezone.utc)
        expires = (
            _as_utc(store_expires_at)  # type: ignore[arg-type]
            if isinstance(store_expires_at, datetime)
            else now + timedelta(days=iap_catalog.period_days(ref.billing_period))
        )
        sub = self.db.scalar(
            select(Subscribe)
            .where(Subscribe.member_id == member_id, Subscribe.is_activate.is_(True))
            .order_by(Subscribe.subscribe_id.desc())
        )
        if sub is not None:
            sub.end_date = expires
            sub.plan = ref.plan or sub.plan
            sub.billing_period = ref.billing_period or sub.billing_period
            sub.product_id = product_id
            sub.source = "store"
            # 스토어가 갱신에 성공했다 = 재시도/보류 상태가 아니다.
            sub.billing_state = "ok"
            sub.retrying_until = None
            sub.paused_since = None
            return expires
        self.db.add(Subscribe(
            member_id=member_id,
            start_date=now,
            end_date=expires,
            price=None,  # 스토어가 청구한다 — 서버는 금액을 모른다
            is_activate=True,
            plan=ref.plan or "pro",
            billing_period=ref.billing_period,
            product_id=product_id,
            source="store",
        ))
        return expires


def _as_utc(dt: datetime) -> datetime:
    """naive datetime 을 UTC 로 간주해 비교 가능하게 만든다(DB 가 tz 를 잃는 경우 대비)."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
