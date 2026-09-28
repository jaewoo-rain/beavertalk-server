"""iap_receipt — 처리한 스토어 영수증 원장(멱등성의 근거).

🧒 왜 필요한가: 같은 영수증이 **여러 번 오는 게 정상**이다 —
   ① 네트워크 재시도 ② 앱 재실행 ③ 구매 복원(폰 교체·재설치).
   기록이 없으면 그때마다 다시 지급하거나(중복 지급) 에러를 뱉는다(정상 흐름 파손).
   그래서 "이 거래를 처리했는가"를 여기 남기고, 다음에 오면 **재지급 없이 성공** 응답한다.

UNIQUE(platform, transaction_id) 가 멱등 키다. 동시에 두 요청이 와도 DB 가 하나만
통과시킨다(애플리케이션 검사만으론 경합에서 샌다).

⚠ member_id 를 함께 저장해 **다른 계정이 같은 영수증을 쓰는 것**을 잡는다(409).
   가족 공유·계정 전환으로 실제로 일어난다.

계약: docs/20260731_1230_IAP-API-계약서-프론트공유용.md
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class IapReceipt(Base, TimestampMixin):
    __tablename__ = "iap_receipt"

    iap_receipt_id: Mapped[int] = mapped_column(
        BigInteger, Identity(), primary_key=True
    )
    # ⛔⛔ S2(2026-09-26, Play 심사 대비) — nullable + ON DELETE SET NULL(옛 CASCADE).
    #   영수증 원장도 결제 보존 의무 대상이라 회원 하드 삭제에 같이 지워지면 안 된다.
    #   같은 커밋의 마이그레이션(c2d4e6f8a0b1)과 반드시 같은 내용이어야 한다(R2) —
    #   sqlite(테스트)는 이 파일의 제약을 쓰고 운영은 그 마이그레이션을 쓴다.
    member_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("member.member_id", ondelete="SET NULL"), index=True,
        comment="지급받은 회원",
    )
    platform: Mapped[str] = mapped_column(Text, comment="ios | android")
    transaction_id: Mapped[str] = mapped_column(
        Text, comment="iOS originalTransactionId / Android orderId"
    )
    product_id: Mapped[str] = mapped_column(Text, comment="스토어 상품 ID")
    kind: Mapped[str] = mapped_column(Text, comment="character | subscription")
    character_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, comment="캐릭터 지급이면 그 id(구독이면 NULL)"
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="구독 만료(캐릭터는 NULL)"
    )
    is_sandbox: Mapped[bool] = mapped_column(
        default=False, comment="테스트 결제 여부(운영 집계에서 제외)"
    )
    # 스텁으로 통과했는지. 자격증명 없이 QA 한 흔적이라 운영 정산에서 걸러야 한다.
    is_stub: Mapped[bool] = mapped_column(
        default=False, comment="스텁 검증(실검증 아님) — 운영 집계 제외"
    )
    # ⭐⭐ §24(2026-09-28) — 스토어 재조회의 전제. 예전엔 verify()→acknowledge() 로만
    #   쓰고 버렸다(core/iap.py 의 verify()·acknowledge() 호출부 참조) — 그래서 갱신을
    #   서버가 능동적으로 확인할 방법이 없었다(구독 상태·권한이 검증 시점 저장값에만
    #   의존, iap_service.entitlement()·subscription_status.resolve_status() 참조).
    #   ⛔ 로그에 절대 찍지 않는다(스토어 자격에 준한다) — core/iap.py·iap_service.py
    #   어디에도 이 값을 interpolate 하는 로그가 없다(회귀로 고정, tests/test_iap.py).
    #   ⛔ 기존 5행은 NULL 로 남는다(백필 불가 — 토큰이 어디에도 없었다). 앱 「구매
    #   복원」을 한 번 누르면 새 receipt 행이 토큰과 함께 채워진다.
    purchase_token: Mapped[Optional[str]] = mapped_column(
        Text, comment="스토어 재조회용(§24). NULL=이 컬럼 추가 이전 receipt. 로그 금지",
    )
    # 재조회 쓰로틀(IAP_RECHECK_MIN_INTERVAL_S) 기준 시각 — 진짜 해지한 회원의
    # end_date 는 영원히 과거라, 이게 없으면 읽을 때마다 스토어를 때린다.
    last_store_check_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="마지막 스토어 재조회 시각(§24 쓰로틀용)",
    )

    __table_args__ = (
        UniqueConstraint("platform", "transaction_id", name="uq_iap_platform_tx"),
        Index("ix_iap_receipt_purchase_token", "purchase_token"),
    )
