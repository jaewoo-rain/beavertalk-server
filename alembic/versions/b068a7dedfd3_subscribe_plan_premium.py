"""구독 2단화 — pro·max → premium (D1)

판매 전이라 데이터·계약을 그대로 바꾼다. 옛 3티어(pro/max) 문자열을 쓰던 행을
전부 premium 으로 합치고, 컬럼 기본값·설명도 맞춘다.

되돌리기는 premium→max 로 되돌린다(pro 였는지 max 였는지는 이미 잃은 정보라 복구
불가 — 판매 전 스텁 데이터만 있던 시점이라 손실 없음).

Revision ID: b068a7dedfd3
Revises: c19efa26e9c8
Create Date: 2026-09-22
"""

from alembic import op
import sqlalchemy as sa


revision = "b068a7dedfd3"
down_revision = "c19efa26e9c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE subscribe SET plan = 'premium' WHERE plan IN ('pro', 'max')")
    op.alter_column(
        "subscribe", "plan",
        existing_type=sa.String(8), existing_nullable=False,
        server_default="premium",
        comment="free 없음 · premium",
    )


def downgrade() -> None:
    op.execute("UPDATE subscribe SET plan = 'max' WHERE plan = 'premium'")
    op.alter_column(
        "subscribe", "plan",
        existing_type=sa.String(8), existing_nullable=False,
        server_default="pro",
        comment="pro | max",
    )
