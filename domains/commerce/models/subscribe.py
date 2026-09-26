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
    # ⛔⛔ S2(2026-09-26, Play 심사 대비) — nullable + ON DELETE SET NULL(옛 CASCADE).
    #   구독 기록은 결제 관련 보존 의무 대상이라 회원 하드 삭제에 같이 지워지면 안 된다.
    #   같은 커밋의 마이그레이션(c2d4e6f8a0b1)과 반드시 같은 내용이어야 한다(R2) —
    #   sqlite(테스트)는 이 파일의 제약을 쓰고 운영은 그 마이그레이션을 쓴다.
    member_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("member.member_id", ondelete="SET NULL"), index=True, comment="회원",
    )
    start_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="시작(결제) 날짜")
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="끝나는 날짜")
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), comment="결제 금액")
    is_activate: Mapped[Optional[bool]] = mapped_column(Boolean, comment="활성화 여부")

    # ── 2단화(2026-09-22, D1) ─────────────────────────────────────────────── #
    # 옛 pro/max 는 모두 premium 이다 — free 는 행이 없다는 뜻이라 이 컬럼엔 애초에
    # 안 나온다(그래서 컬럼 자체엔 free 가 없다). 판매 전이라 데이터를 그대로 바꿨다.
    #
    # ⛔ 상태(state)와 플랜(plan)은 **다른 축**이다(3티어 재편 2026-08-04 의 설계가
    #   그대로 남는다). 상태 8종 → D1 로 active_pro/active_max 가 active_premium 하나로
    #   합쳐져 지금은 7종(free/trial/active_premium/grace/on_hold/ending/expired).
    #   grace/on_hold/ending 은 "직전에 무슨 플랜이었는지"를 유지하므로 상태만으로
    #   플랜을 알 수 없다. 앱도 같은 구조로 짜여 있다(subscription_state.dart —
    #   impliedTier 가 이 세 상태에서 null).
    plan: Mapped[str] = mapped_column(
        String(8), server_default="premium", nullable=False, comment="free 없음 · premium",
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

    # ⛔ Optional — SET NULL 대상이라 탈퇴 회원의 구독 행은 이 관계가 None 이 된다.
    member: Mapped[Optional["Member"]] = relationship(back_populates="subscribes")
