"""subscribe (구독) — commerce 도메인. member 와 N:1."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.account.models.member import Member


class Subscribe(Base, TimestampMixin):
    __tablename__ = "subscribe"

    subscribe_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE"), index=True, comment="회원",
    )
    start_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="시작(결제) 날짜")
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="끝나는 날짜")
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), comment="결제 금액")
    is_activate: Mapped[Optional[bool]] = mapped_column(Boolean, comment="활성화 여부")

    # ── 3티어 재편(2026-08-04) ──────────────────────────────────────────── #
    # 왜 필드가 늘었나: 디자인이 상태 8종(free/trial/active_pro/active_max/grace/
    # on_hold/ending/expired)을 전제하는데, 위 5필드로 판정 가능한 건 4종뿐이었다.
    # 나머지는 서버에 데이터가 아예 없어 앱이 원천적으로 판정 불가였다.
    #
    # ⛔ 상태(state)와 플랜(plan)은 **다른 축**이다. grace/on_hold/ending 은 "직전에
    #   무슨 플랜이었는지"를 유지하므로, 상태만으로 플랜을 알 수 없다. 앱도 같은 구조로
    #   짜여 있다(subscription_state.dart — impliedTier 가 이 세 상태에서 null).
    plan: Mapped[str] = mapped_column(
        String(8), server_default="pro", nullable=False, comment="pro | max",
    )
    billing_period: Mapped[Optional[str]] = mapped_column(
        String(8), comment="monthly | yearly (스토어 상품에서 파생)",
    )
    # 스토어가 준 상품 ID 원본. plan·billing_period 는 여기서 파생되는 캐시다
    # ("증거가 원본·나머지는 파생" — 레벨 시스템 item_evidence 와 같은 규율).
    # 결제 없이 만든 행(source=manual)은 스토어 상품이 없으므로 NULL.
    product_id: Mapped[Optional[str]] = mapped_column(Text, comment="스토어 상품 ID 원본")
    # ⭐ 가짜/진짜 구분. 결제 미연동 기간에 만든 행과 스토어가 준 행이 같은 테이블에
    #   섞인다 — 이 컬럼이 없으면 결제가 붙는 날 "누가 진짜 유료인가"를 못 가른다
    #   (iap_receipt.is_stub 과 같은 이유: 운영 정산에서 걸러야 한다).
    source: Mapped[str] = mapped_column(
        String(8), server_default="manual", nullable=False, comment="manual | store",
    )
    is_trial: Mapped[bool] = mapped_column(
        Boolean, server_default="false", nullable=False,
        comment="체험 기간인가(앱은 체험을 Max 로 취급)",
    )
    # 결제 재시도 상태. 스토어 서버만 알 수 있는 값이라, 폴링/웹훅이 붙기 전까지는
    # 항상 'ok' 다. 컬럼을 먼저 두는 이유는 앱 계약이 이미 이 값을 전제하기 때문.
    billing_state: Mapped[str] = mapped_column(
        String(16), server_default="ok", nullable=False, comment="ok | grace | on_hold",
    )
    # 화면 문구 "Retrying until …" / "Paused since …" 의 원천. 앱이 자체 계산하지
    # 않고 **서버 값을 그대로 쓴다**(기기 시계 조작·시차 방지).
    retrying_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="grace 에서만 값 존재",
    )
    paused_since: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="on_hold 에서만 값 존재",
    )

    member: Mapped["Member"] = relationship(back_populates="subscribes")
