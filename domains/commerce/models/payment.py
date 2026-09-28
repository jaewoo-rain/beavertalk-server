"""payment (결제) — commerce 도메인. member 와 N:1."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.account.models.member import Member


class Payment(Base, TimestampMixin):
    __tablename__ = "payment"

    payment_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # ⛔⛔ S2(2026-09-26, Play 심사 대비) — nullable + ON DELETE SET NULL(옛 CASCADE).
    #   결제 기록은 법무 보존 의무 대상이라 회원 하드 삭제에 같이 지워지면 안 된다.
    #   같은 커밋의 마이그레이션(c2d4e6f8a0b1)과 반드시 같은 내용이어야 한다(R2) —
    #   sqlite(테스트)는 이 파일의 제약을 쓰고 운영은 그 마이그레이션을 쓴다.
    member_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("member.member_id", ondelete="SET NULL"), index=True, comment="회원",
    )
    payment_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="결제 날짜")
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), comment="결제 금액")
    description: Mapped[Optional[str]] = mapped_column(Text, comment="결제 내용")
    category: Mapped[Optional[str]] = mapped_column(
        Text, index=True, comment="결제 분류(subscribe/character)"
    )
    card_info: Mapped[Optional[str]] = mapped_column(Text, comment="카드 정보(마스킹)")

    # ⭐⭐ §22-⑥(2026-09-28) — 스토어 결제(구독·캐릭터·묶음)를 결제 내역에 남기기
    #   위해 추가. ⛔ 기존 price(달러 Numeric, 수동 결제 시절부터 있던 값)를
    #   갈아치우지 않고 **곁에 추가**한다 — 기존 행·수동 결제와 섞이면 안 된다.
    #   ⚠ price 는 currency=='USD' 일 때만 이 두 필드에서 환산해 채운다(1:1, 환율
    #   변환 없음) — 그 외 통화는 price 가 계속 NULL 이라 "이번 달 결제" 합계에서
    #   빠진다(환율 변환은 범위 밖, YAGNI — 필요해지면 그때 추가).
    local_currency: Mapped[Optional[str]] = mapped_column(
        Text, comment="스토어가 청구한 통화(ISO 4217, 예: USD) — local_price_micros 와 짝",
    )
    local_price_micros: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        comment="스토어가 청구한 현지 금액(micros, 1,000,000=1단위). Google 캐릭터·묶음"
                "(ProductPurchase)은 스토어가 이 값을 안 줘서 NULL이 정상(core/iap.py 참조)",
    )
    store_order_id: Mapped[Optional[str]] = mapped_column(
        Text, index=True, comment="스토어 주문 ID(Google orderId / Apple transactionId)",
    )
    # ⭐⭐ iap_receipt.is_sandbox/is_stub 과 같은 규율("저장 + 표식 + 집계 제외") —
    #   행은 남기되(결제 내역 목록엔 그대로 보인다) PaymentRepository.month_total
    #   (운영 집계)에서만 제외한다. 완전히 안 남기면 "정말 결제가 안 됐나 테스트
    #   결제로 걸러졌나"를 나중에 구분할 수 없다.
    is_sandbox: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
        comment="테스트 결제 여부(월 합계 제외) — iap_receipt.is_sandbox 와 같은 규율",
    )
    is_stub: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
        comment="스텁 검증(실검증 아님, 월 합계 제외)",
    )

    # ⛔ Optional — SET NULL 대상이라 탈퇴 회원의 결제 행은 이 관계가 None 이 된다.
    member: Mapped[Optional["Member"]] = relationship(back_populates="payments")
