# -*- coding: utf-8 -*-
"""payment.local_currency/local_price_micros/store_order_id/is_sandbox/is_stub(§22-⑥)

앱 요청서 §5 표 §22-⑥. 실결제가 결제 내역에 안 나오고 앱이 "이번 달 결제 $0"으로
보였다 — 스토어 결제(POST /purchases/verify)가 payment 행을 만들지 않았다(옛
무료 지급 경로 purchase_service.py 만 만들고 있었다, 그 경로는 별도로 삭제된다).

⛔ 기존 price(달러 Numeric)는 갈아치우지 않고 곁에 추가한다 — 기존 행·수동 결제와
섞이면 안 된다. local_currency·local_price_micros 는 스토어가 청구한 현지 통화·
금액(micros)이고, price 는 currency=='USD' 일 때만 그 값에서 환산해 채운다(1:1,
환율 변환 없음).

is_sandbox/is_stub 은 iap_receipt 의 같은 이름 플래그와 같은 규율 — 저장은 하되
(결제 내역 목록엔 보인다) 월 합계 집계에서만 제외한다.

⛔ 백필 없음 — 기존 payment 행(수동 결제 시절)은 스토어 필드가 원천에 없다.

Revision ID: a5b1e6ca8a67
Revises: 2d65f20ea0c8
"""

from alembic import op
import sqlalchemy as sa

revision = "a5b1e6ca8a67"
# ⚠ 원래 f737b32e72cf(§25-① tz_offset_min 정리) 였으나, appreq-b 의 3건(member.tz·
#   call.summary_lang·cur_text_i18n)이 먼저 적용·푸시되며 운영 head 가
#   f737b32e72cf → 4cec70d04fc0 → 2d65f20ea0c8 로 옮겨졌다(bt-back 지시, 2026-09-29)
#   — 그 위로 다시 엮는다.
down_revision = "2d65f20ea0c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payment",
        sa.Column(
            "local_currency", sa.Text(), nullable=True,
            comment="스토어가 청구한 통화(ISO 4217, 예: USD) — local_price_micros 와 짝",
        ),
    )
    op.add_column(
        "payment",
        sa.Column(
            "local_price_micros", sa.BigInteger(), nullable=True,
            comment="스토어가 청구한 현지 금액(micros, 1,000,000=1단위). Google 캐릭터·묶음"
                    "(ProductPurchase)은 스토어가 이 값을 안 줘서 NULL이 정상",
        ),
    )
    op.add_column(
        "payment",
        sa.Column(
            "store_order_id", sa.Text(), nullable=True,
            comment="스토어 주문 ID(Google orderId / Apple transactionId)",
        ),
    )
    op.add_column(
        "payment",
        sa.Column(
            "is_sandbox", sa.Boolean(), nullable=False, server_default=sa.text("false"),
            comment="테스트 결제 여부(월 합계 제외) — iap_receipt.is_sandbox 와 같은 규율",
        ),
    )
    op.add_column(
        "payment",
        sa.Column(
            "is_stub", sa.Boolean(), nullable=False, server_default=sa.text("false"),
            comment="스텁 검증(실검증 아님, 월 합계 제외)",
        ),
    )
    op.create_index("ix_payment_store_order_id", "payment", ["store_order_id"])


def downgrade() -> None:
    op.drop_index("ix_payment_store_order_id", table_name="payment")
    op.drop_column("payment", "is_stub")
    op.drop_column("payment", "is_sandbox")
    op.drop_column("payment", "store_order_id")
    op.drop_column("payment", "local_price_micros")
    op.drop_column("payment", "local_currency")
