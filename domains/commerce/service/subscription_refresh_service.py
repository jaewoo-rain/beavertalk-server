"""SubscriptionRefreshService — 만료된 store 구독을 스토어에 재조회해 반영(§24).

🧒 문제: 갱신을 받는 경로가 0건이라(RTDN·Server Notifications 전부 미구현) 구독
    상태는 검증(verify_and_grant) 시점에 저장한 end_date 에서 멈춰 있다. 운영
    store 구독 4/4건이 그래서 전부 만료로 보였다(실제로는 계속 자동갱신 중일 수
    있다) — 앱팀이 지목한 출시 차단 버그.

해법: core/iap.py 의 verify() 가 이미 스토어에서 최신 만료 시각을 받아온다 —
    "갱신 반영" 은 곧 **같은 verify() 를 다시 부르는 것**이다. 새 스토어 API 가
    필요 없다. 입구 4개(읽을 때 자기치유·스케줄 스윕·복원/재검증·나중의 RTDN)가
    **갱신 함수 1개**(`_bump_end_date_if_later`)를 공유한다 — 알림은 트리거일
    뿐, 진실은 항상 스토어 재조회다(RTDN 자체는 이번 범위 밖).

⭐⭐ 입구③ — 복원/재검증(already_granted, 2026-09-28 bt-back 실측 추가):
    `iap_service.verify_and_grant` 의 멱등 체크(이미 처리한 거래)는 `iap.verify()`
    **뒤**에 있다 — 즉 앱이 같은 영수증으로 재검증/복원을 보내면 **스토어에 다시
    묻고 있으면서도**, 방금 받은 새 `expires_at` 을 already_granted 로 그냥
    버렸다. 운영 로그 실증: `bt_max_monthly` 검증이 3회 찍혔는데 그 상품의
    android 영수증 행은 2개뿐(한 번은 already_granted 경로 — 그때도 core.iap 의
    sandbox 불일치 로그가 찍혀 verify 가 실제로 돌았음을 확인). 이게 "재검증해도
    만료가 안 바뀐다"로 관찰된 증상의 정확한 원인이었다. `bump_subscription_
    from_verify_result` 가 이 입구를 맡는다 — `iap_service.py` 가 이미 들고 있는
    `VerifyResult` 를 그대로 넘기므로 **verify() 를 또 부르지 않는다.**
    ⭐ 동시에 이 입구는 **기존 5행(토큰 없음)의 유일한 이주 경로**이기도 하다 —
    앱 「구매 복원」 버튼(이미 있음, 앱 수정 불필요) 한 번이면 그 receipt 의
    purchase_token 이 채워지고, 그 뒤부터 입구①②의 자기치유가 그 회원에게도
    작동한다(purchase_token 채우기는 `iap_service.py` 가 한다 — 그 값의 원본인
    `PurchaseItem` 은 이 모듈이 모른다).

⛔⛔ 절대 틀리면 안 되는 4가지(bt-back, §24 지시):
    1. source='store' 인 행만 재조회한다. manual(사장님이 직접 준 Premium)을
       스토어에 물으면 전부 invalid 가 나와 날아간다 — `_refresh_from_store` 의
       첫 줄 가드가 이것이다.
    2. reason='unavailable'(스토어 장애)엔 박탈하지 않는다 — 저장값을 그대로
       두고 다음에 재시도한다. invalid 일 때만(사실상 아무것도 안 해도) Free다 —
       end_date 가 이미 과거이므로 판정 함수(resolve_status/entitlement)가
       그 자체로 Free 를 낸다.
    3. 통화 경로(entitlements.effective_plan)는 이 모듈을 **전혀 참조하지
       않는다** — 통화가 스토어 장애에 묶이면 안 된다. 앱이 시작할 때 읽는
       두 자리(SubscriptionService.status·IapService.entitlement)에서만
       치유하고, 통화는 치유된 저장값(Subscribe.end_date)을 읽는다.
    4. purchase_token 을 로그에 찍지 않는다(스토어 자격에 준한다).

설계: docs/plans/2026-09-28-구독갱신-반영-24.md §3.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import iap
from core.config import settings
from domains.commerce.models.iap_receipt import IapReceipt
from domains.commerce.models.subscribe import Subscribe

logger = logging.getLogger(__name__)


def _as_utc(dt: datetime) -> datetime:
    """naive datetime 을 UTC 로 간주(DB 가 tz 를 잃는 경우 대비 — iap_service 와 동일)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _latest_subscription_receipt(db: Session, member_id: Optional[int]) -> Optional[IapReceipt]:
    """이 회원의 가장 최근 구독 영수증(재조회 열쇠 — platform/product_id/transaction_id/token)."""
    if member_id is None:  # 탈퇴 회원(S2, SET NULL) — 재조회 대상 아님
        return None
    return db.scalar(
        select(IapReceipt)
        .where(IapReceipt.member_id == member_id, IapReceipt.kind == "subscription")
        .order_by(IapReceipt.iap_receipt_id.desc())
    )


def _bump_end_date_if_later(sub: Subscribe, expires_at: Optional[datetime]) -> bool:
    """§24 갱신 함수(입구 3개가 공유) — expires_at 이 현재 저장값보다 나중이면 올린다.

    ⛔ **한 방향만.** 박탈은 이 함수의 일이 아니다(호출부가 이미 "박탈 안 함" 분기를
    스스로 처리했거나, 애초에 ok=True 일 때만 이 함수를 부른다) — 스토어 응답의
    시각이 어떤 이유로든(시계 스큐 등) 저장값보다 앞이면 조용히 무시한다.
    """
    if expires_at is None:
        return False
    if sub.end_date is not None and _as_utc(expires_at) <= _as_utc(sub.end_date):
        return False
    sub.end_date = expires_at
    return True


def bump_subscription_from_verify_result(
    db: Session, member_id: int, result: iap.VerifyResult
) -> bool:
    """입구③(복원/재검증 — `iap_service.verify_and_grant` 의 already_granted 분기)
    전용. 이미 손에 든 `VerifyResult`(호출부가 ok=True 를 보장한다 — ②에서
    ok=False 는 이미 422/503 으로 끝나 여기 도달하지 않는다)로 그 회원의 store
    구독 만료를 올린다. **verify() 를 다시 부르지 않는다** — 결과를 재사용할 뿐.

    ⛔ source='store' + is_activate=True 인 행만 대상(최우선 안전장치, 위 모듈
    docstring 규율 1번과 동일). 소유자 불일치(409) 분기는 호출부가 이 함수를
    아예 안 부른다 — 여기서 다시 검사하지 않는다.
    """
    if result.expires_at is None:
        return False
    sub = db.scalar(
        select(Subscribe)
        .where(
            Subscribe.member_id == member_id, Subscribe.source == "store",
            Subscribe.is_activate.is_(True),
        )
        .order_by(Subscribe.subscribe_id.desc())
    )
    if sub is None:
        return False
    bumped = _bump_end_date_if_later(sub, result.expires_at)
    if bumped:
        logger.info(
            "iap 재검증(already_granted): member=%s subscribe=%s 만료 갱신(신규 end_date=%s)",
            member_id, sub.subscribe_id, result.expires_at,
        )
    return bumped


def _refresh_from_store(db: Session, sub: Subscribe) -> bool:
    """이 구독 행을 필요하면 스토어에 재조회해 반영한다. 실제로 스토어를 불렀으면 True.

    가드 순서(전부 "조용히 통과", 예외 없음):
      ① source != 'store'         → manual 은 절대 건드리지 않는다(최우선 안전장치)
      ② 아직 안 지남(무기한 포함)  → 재조회 불필요
      ③ 영수증·토큰 없음          → 기존 5행(백필 불가) — 앱 「복원」 대기
      ④ 쓰로틀 안                 → 진짜 해지한 회원은 end_date 가 영원히 과거라
                                     쓰로틀 없이는 매 호출 스토어를 때린다
    """
    if sub.source != "store":
        return False
    now = datetime.now(timezone.utc)
    if sub.end_date is None or _as_utc(sub.end_date) > now:
        return False
    receipt = _latest_subscription_receipt(db, sub.member_id)
    if receipt is None or not receipt.purchase_token:
        return False
    if receipt.last_store_check_at is not None:
        elapsed = (now - _as_utc(receipt.last_store_check_at)).total_seconds()
        if elapsed < settings.IAP_RECHECK_MIN_INTERVAL_S:
            return False

    result = iap.verify(
        platform=receipt.platform,  # type: ignore[arg-type]
        kind="subscription",
        product_id=receipt.product_id,
        transaction_id=receipt.transaction_id,
        purchase_token=receipt.purchase_token,
        is_sandbox=receipt.is_sandbox,
    )
    receipt.last_store_check_at = now
    if result.ok:
        _bump_end_date_if_later(sub, result.expires_at)
        logger.info(
            "iap 재조회: member=%s subscribe=%s 갱신 반영(신규 end_date=%s)",
            sub.member_id, sub.subscribe_id, result.expires_at,
        )
    elif result.reason == "invalid":
        # ⛔ 여기서 아무것도 더 안 바꾼다 — end_date 가 이미 과거이므로
        #   resolve_status/entitlement 가 그 자체로 Free 를 낸다(위 docstring ②).
        logger.info(
            "iap 재조회: member=%s subscribe=%s invalid(진짜 종료 — 해지/환불/만료)",
            sub.member_id, sub.subscribe_id,
        )
    else:  # "unavailable" — ⛔ 박탈 금지. 저장값 그대로 두고 다음에 재시도.
        logger.info(
            "iap 재조회: member=%s subscribe=%s unavailable(스토어 응답 없음) — 박탈 안 함, 재시도 예정",
            sub.member_id, sub.subscribe_id,
        )
    db.commit()
    return True


class SubscriptionRefreshService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def refresh_member(self, member_id: int) -> None:
        """읽을 때(2단계) 자기치유 — `/subscriptions/status`·`/purchases/entitlement`
        진입 시 이 회원의 store 구독을 필요하면(위 가드 참조) 재조회한다.

        ⛔ 통화 경로(entitlements.effective_plan)는 이 메서드를 부르지 않는다 —
        위 모듈 docstring ③ 참조.
        """
        subs = self.db.scalars(
            select(Subscribe).where(
                Subscribe.member_id == member_id, Subscribe.source == "store",
            )
        ).all()
        for sub in subs:
            _refresh_from_store(self.db, sub)

    def sweep(self) -> int:
        """스케줄 스윕(3단계) — 만료된 store 구독 전체를 훑는다(앱을 안 열어도 반영).

        Returns:
            실제로 스토어를 불러 재조회를 시도한 건수(가드에 걸려 건너뛴 건 제외).
        """
        now = datetime.now(timezone.utc)
        subs = self.db.scalars(
            select(Subscribe).where(
                Subscribe.source == "store",
                Subscribe.is_activate.is_(True),
                Subscribe.end_date.is_not(None),
                Subscribe.end_date <= now,
            )
        ).all()
        return sum(1 for sub in subs if _refresh_from_store(self.db, sub))
