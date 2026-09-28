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
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core import iap
from domains.commerce.models.iap_receipt import IapReceipt
from domains.commerce.models.member_character import MemberCharacter
from domains.commerce.models.payment import Payment
from domains.commerce.models.subscribe import Subscribe
from domains.commerce.schemas.iap import (
    Entitlement,
    PurchaseItem,
    RestoreItemResult,
    RestoreResponse,
    VerifyResponse,
)
from domains.commerce.service import iap_catalog
from domains.commerce.service import subscription_refresh_service

logger = logging.getLogger(__name__)

# ⭐⭐ §22-③(2026-09-28) — verify_and_grant 이 던지는 HTTPException → 요청서가
# 정한 5종 사유. ⛔⛔ 확인 결과, **5종이 실제 코드 경로와 완전히 1:1 은 아니다**:
# 404 UNKNOWN_PRODUCT(우리 서버가 모르는 product_id — iap_catalog.resolve 가 못
# 찾음)는 이 5종 목록에 없다. "invalid"(스토어가 무효 판정)와는 출처가 다르지만
# (우리 카탈로그 미스 vs 스토어 거절), 앱 입장에서는 둘 다 "재시도해도 안 됨"이라
# 실질이 같아 invalid 로 접는다 — 억지로 6번째 사유를 만들지 않는다(요청서가
# 정한 Literal 을 그대로 지킨다). bt-back 에게 이 매핑을 판단으로 보고했다.
_RESTORE_FAILURE_REASON = {
    status.HTTP_409_CONFLICT: "owned_by_other",
    status.HTTP_422_UNPROCESSABLE_ENTITY: "invalid",
    status.HTTP_503_SERVICE_UNAVAILABLE: "unavailable",
    status.HTTP_404_NOT_FOUND: "invalid",  # UNKNOWN_PRODUCT — 위 주석 참조
}


class IapService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ── 조회 ────────────────────────────────────────────────────────────── #
    def entitlement(self, member_id: int) -> Entitlement:
        """이 회원이 **지금** 가진 권한. "Pro 인가"의 단일 진실.

        만료 판정을 서버가 한다 — 앱이 만료 시각을 자체 비교하면 기기 시계 조작·시차로
        어긋난다. 앱은 is_pro 를 그대로 쓴다.

        ⭐⭐ §24 입구①(2026-09-28) — 판정 전에 이 회원의 store 구독을 필요하면(§24
        가드 — source='store'·만료됨·토큰 있음·쓰로틀 통과) 재조회해 반영한다.
        앱이 시작할 때 이 엔드포인트를 부르니 여기서 치유되면 충분하다 — ⛔ **통화
        경로(entitlements.effective_plan)는 이 재조회를 타지 않는다**(별도 함수,
        subscription_refresh_service 를 참조하지 않는다) — 통화가 스토어 장애에
        묶이면 안 된다.
        """
        subscription_refresh_service.SubscriptionRefreshService(self.db).refresh_member(member_id)
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
        *,
        _via_restore: bool = False,
    ) -> VerifyResponse:
        # ① 상품 해석
        ref = iap_catalog.resolve(self.db, item.product_id)
        if ref is None:
            # ⭐⭐ §22-③ 보완(2026-09-29, bt-back 지시) — 이 404 는 스토어 거절이
            #   아니라 **우리 카탈로그(iap_catalog.resolve)에 그 상품이 없다**는
            #   뜻이다 — 서버 쪽 구멍이다. bt_character_bundle 이 정확히 이
            #   경로였다(PRODUCT_PREFIX_CHARACTER 접두사 분기에 먼저 걸려 404) —
            #   앱팀이 보고해 주기 전까지 서버는 몰랐다. §22-③ 응답에서는
            #   invalid 로 뭉치지만(앱 enum 5종 계약 유지, §17 otherApp/other_app
            #   과 같은 이유로 6번째 값을 안 만든다), 그러면 다음에 같은 일이
            #   나도 우리가 모른다 — 그래서 여기서 따로 남긴다.
            #   ⛔ §23 로그(core/iap.py, 스토어 왕복)는 여기 안 걸린다 — resolve
            #   실패는 스토어에 묻기도 전에 끝난다. ⛔ purchase_token 은 인자로도
            #   안 받아서 안 찍는다(정적 스캔 시험이 지킨다).
            logger.warning(
                "iap: UNKNOWN_PRODUCT product=%s platform=%s member=%s 경로=%s — "
                "스토어 거절이 아니라 우리 카탈로그에 없다(서버 쪽 구멍 의심)",
                item.product_id, platform, member_id, "restore" if _via_restore else "verify",
            )
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
            kind=ref.kind,  # type: ignore[arg-type]
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
            # ⭐⭐ §24 입구③(2026-09-28, bt-back 실측) — 위 ②의 verify() 가 이미
            #   스토어에 다시 물었고(여기 도달 = result.ok=True, ok=False 면 이미
            #   422/503 으로 끝났다) 새 expires_at 을 손에 들고 있다. 예전엔 그걸
            #   버리고 already_granted 만 응답했다 — "재검증해도 구독 만료가 안
            #   바뀐다"로 관찰된 버그의 정확한 원인이다(운영 로그 실증: bt_max_monthly
            #   검증 3회·영수증 행 2개, 한 번이 이 경로였다). verify() 를 또 부르지
            #   않고 이미 가진 result 를 그대로 반영한다(subscription_refresh_service
            #   와 갱신 함수 1개 공유 — 로직 두 벌 금지).
            if not existing.purchase_token:
                # ⭐ 기존 5행(토큰 없음)의 유일한 이주 경로 — 앱 「복원」(이미 있는
                #   버튼, 앱 수정 불필요) 한 번이면 여기서 채워지고, 그 뒤부터
                #   입구①②(읽을 때 자기치유·스케줄 스윕)가 이 회원에게도 작동한다.
                existing.purchase_token = item.purchase_token
            if existing.kind == "subscription":
                subscription_refresh_service.bump_subscription_from_verify_result(
                    self.db, member_id, result,
                )
                # ⛔ 쓰로틀 기록도 여기서 남긴다 — 안 남기면 바로 아래 entitlement()
                #   호출이 입구①(자기치유)을 다시 태워 **같은 요청 안에서 verify()
                #   를 두 번** 부른다(갱신이 실제로 안 붙었을 때만 재현 — 안 붙었으면
                #   end_date 가 여전히 과거라 입구①의 "만료됨" 가드를 못 피한다).
                existing.last_store_check_at = datetime.now(timezone.utc)
            self.db.commit()
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
        elif ref.kind == "bundle":
            self._grant_bundle(member_id)
        else:
            expires_at = self._grant_subscription(
                member_id, result.expires_at, ref, item.product_id, result.is_trial,
            )

        self.db.add(IapReceipt(
            member_id=member_id,
            platform=platform,
            transaction_id=tx_id,
            product_id=item.product_id,
            kind=ref.kind,
            character_id=ref.character_id,
            expires_at=expires_at,
            # ⭐ S1(2026-09-27) — 클라 self-report(is_sandbox) OR 스토어 실측
            # (result.store_confirmed_test, 라이선스 테스트 계정·StoreKit 샌드박스).
            # 클라가 그 사실을 숨겨도(예: false 로 보내도) 스토어가 확인해 준 값이
            # 잡는다 — 누구나 가입 가능한 라이선스 테스트 그룹으로 받은 Premium 이
            # 매출 집계에 조용히 섞여 들어가지 않게(iap.verify 가 어긋나면 로그도 남긴다).
            is_sandbox=is_sandbox or result.store_confirmed_test,
            is_stub=result.stubbed,
            # ⭐⭐ §24(2026-09-28) — 재조회(subscription_refresh_service)의 유일한
            #   열쇠. ⛔ 로그에 절대 찍지 않는다(스토어 자격에 준한다).
            purchase_token=item.purchase_token,
        ))
        # ⭐⭐ §22-⑥(2026-09-28) — 스토어 결제를 결제 내역(payment)에 남긴다. 옛
        #   무료 지급 경로(purchase_service.py)만 payment 행을 만들고 있었고 이
        #   verify_and_grant(진짜 결제 경로)는 안 만들어서 "이번 달 결제 $0"으로
        #   보이는 버그였다. 구독·캐릭터·묶음 전부 여기서 한 트랜잭션으로 같이 남긴다.
        self.db.add(_build_payment(member_id, ref, item, result, is_sandbox))
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
        # ⛔⛔ S1(2026-09-26, Play 심사 대비) — acknowledge 는 지급(위 commit)이 끝난
        #   **다음에만** 부른다(iap.acknowledge 의 docstring 참조 — 순서가 곧 환불
        #   방지 설계). 실패해도 이미 커밋된 지급을 되돌리지 않는다(R5) — 구글이 3일
        #   여유를 준다. already_granted(멱등 재조회) 경로는 이미 한 번 지급 때
        #   acknowledge 를 불렀을 것이므로 여기서 다시 부르지 않는다.
        if not iap.acknowledge(platform, ref.kind, item.purchase_token, item.product_id):  # type: ignore[arg-type]
            logger.warning(
                "iap: acknowledge 실패(지급은 유지) member=%s tx=%s", member_id, tx_id,
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

        ⭐⭐ §22-③(2026-09-28) — restored/failed(요약 카운트)만으론 "그 캐릭터
        하나가 왜 안 됐는지" 를 앱이 알 수 없었다. items 에 건별 사유를 같이
        낸다(RestoreItemResult 참조) — restored/failed 는 그대로 둔다(추가만,
        구버전 앱 호환).
        """
        restored = failed = 0
        items: list[RestoreItemResult] = []
        for p in purchases:
            try:
                res = self.verify_and_grant(member_id, platform, p, is_sandbox, _via_restore=True)
                if res.already_granted:
                    # already_granted 도 복원 성공이다(이미 갖고 있다는 뜻) — 다만
                    # "새로 지급"은 아니므로 restored 카운트는 안 올린다(기존 규율 유지).
                    items.append(RestoreItemResult(product_id=p.product_id, result="already"))
                else:
                    restored += 1
                    items.append(RestoreItemResult(product_id=p.product_id, result="granted"))
            except HTTPException as exc:
                failed += 1
                reason = _RESTORE_FAILURE_REASON.get(exc.status_code, "invalid")
                items.append(RestoreItemResult(product_id=p.product_id, result=reason))
                logger.info(
                    "iap(restore): 건너뜀 product=%s status=%s reason=%s",
                    p.product_id, exc.status_code, reason,
                )
        return RestoreResponse(
            restored=restored, failed=failed, entitlement=self.entitlement(member_id),
            items=items,
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

    def _grant_bundle(self, member_id: int) -> None:
        """묶음(§2, 2026-09-28) — **없는 것만** 만든다(멱등). 구성 개수만큼을 한 행에
        못 담아 iap_receipt.character_id 는 NULL 로 남는다(kind="bundle" 이 구성을
        대신한다). 구성 자체(지금 character.in_bundle=True 인 캐릭터 전부)는
        iap_catalog.resolve_bundle_character_ids 가 정한다 — 하드코딩 없음, 그
        함수 docstring 참조(소급 지급 걱정이 없는 이유도 거기 있다).

        ⭐ 사장님 확정: 앱은 유료 캐릭터를 하나라도 가진 회원에게 이 상품을 안
        보여준다(fail-closed). ⛔ 그래도 서버는 거절하지 않는다 — 스토어 결제가
        이미 끝난 뒤 도착하는 요청(구버전 앱·경합)을 거절하면 돈만 받고 지급을
        안 하게 된다. `_grant_character` 가 이미 가진 캐릭터에 조용히 통과하므로
        이 함수는 그냥 구성을 순서대로 부르기만 하면 된다 — 있으면 스킵, 없으면 지급.
        """
        for character_id in iap_catalog.resolve_bundle_character_ids(self.db):
            self._grant_character(member_id, character_id)

    def _grant_subscription(
        self,
        member_id: int,
        store_expires_at: object | None,
        ref: iap_catalog.ProductRef,
        product_id: str,
        is_trial: bool = False,
    ) -> datetime:
        """구독 활성화. 만료는 **스토어 값이 우선**, 없으면 주기별 폴백(스텁용).

        기존 활성 구독이 있으면 만료를 연장하고 **플랜·주기도 갱신**한다 — 월납→연납
        전환이 같은 경로로 들어오는데, 만료만 늘리면 새 주기를 안 반영하게 된다
        (D1 이후 plan 은 premium 하나뿐이라 플랜 자체가 바뀌는 일은 없다).

        source='store': 결제 미연동 기간에 만든 행(manual)과 구분하는 표식이다.
        이게 없으면 결제가 붙는 날 "누가 진짜 유료인가"를 못 가른다.

        is_trial: §22-⑤⑦(2026-09-28) — verify() 가 이미 판정해 준 값을 그대로
        저장한다(체험→유료 전환도 같은 경로로 들어온다 — 전환 시 스토어가 offer
        없는 갱신을 주므로 is_trial=False 로 자연히 꺼진다, 별도 처리 불필요).
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
            sub.is_trial = is_trial
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
            plan=ref.plan or "premium",
            billing_period=ref.billing_period,
            product_id=product_id,
            source="store",
            is_trial=is_trial,
        ))
        return expires


def _as_utc(dt: datetime) -> datetime:
    """naive datetime 을 UTC 로 간주해 비교 가능하게 만든다(DB 가 tz 를 잃는 경우 대비)."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_PAYMENT_CATEGORY = {"subscription": "subscribe", "character": "character", "bundle": "character"}
_PAYMENT_LABEL = {"subscription": "구독 결제", "character": "캐릭터 구매", "bundle": "캐릭터 묶음 구매"}


def _build_payment(
    member_id: int,
    ref: iap_catalog.ProductRef,
    item: PurchaseItem,
    result: iap.VerifyResult,
    is_sandbox: bool,
) -> Payment:
    """§22-⑥ — 검증 성공 결과를 결제 내역 1행으로 옮긴다.

    ⛔ category 는 기존 두 값(subscribe/character)만 쓴다 — PaymentType 이 앱 탭
    계약이라 "bundle" 탭이 없다(요청서도 안 시켰다). 묶음은 "캐릭터" 탭에 같이
    보인다(실제로 캐릭터를 지급하는 거래이므로 자연스럽다).

    price(기존 달러 Numeric)는 통화가 USD 일 때만 micros 에서 1:1 환산해 채운다
    (환율 변환 없음 — 범위 밖). 그 외 통화는 price=None 으로 남아 "이번 달 결제"
    합계에서 빠진다 — 환율 변환이 필요해지면 그때 추가한다(YAGNI).
    """
    price_usd: Optional[Decimal] = None
    if result.price_amount_micros is not None and (result.price_currency or "").upper() == "USD":
        price_usd = Decimal(result.price_amount_micros) / Decimal(1_000_000)
    return Payment(
        member_id=member_id,
        payment_date=datetime.now(timezone.utc),
        price=price_usd,
        description=f"{_PAYMENT_LABEL.get(ref.kind, ref.kind)} · {item.product_id}",
        category=_PAYMENT_CATEGORY.get(ref.kind, ref.kind),
        local_currency=result.price_currency,
        local_price_micros=result.price_amount_micros,
        store_order_id=result.order_id,
        # ⭐ IapReceipt 와 같은 OR 규율 — 클라가 숨겨도 스토어 실측이 잡는다.
        is_sandbox=is_sandbox or result.store_confirmed_test,
        is_stub=result.stubbed,
    )
