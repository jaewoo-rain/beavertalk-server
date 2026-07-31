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
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class IapReceipt(Base, TimestampMixin):
    __tablename__ = "iap_receipt"
    __table_args__ = (
        # 멱등 키 — 같은 거래는 플랫폼당 한 번만 처리된다.
        UniqueConstraint("platform", "transaction_id", name="uq_iap_platform_tx"),
    )

    iap_receipt_id: Mapped[int] = mapped_column(
        BigInteger, Identity(), primary_key=True
    )
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE"), index=True,
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
