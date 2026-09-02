"""통화 요약의 교사 로케일 번역 캐시.

`call.summary` 는 학습자 로케일로 1회 생성된다. 교사가 다른 언어의 콘솔에서 읽으려면
번역이 필요한데, 통화 시점에는 콘솔 언어를 알 수 없다. 교사가 **열 때 1회** 번역해
여기 캐시한다. 원본(`call.summary`)은 덮어쓰지 않는다.

설계: 23_제품기획_하네스/_output/2026-08-19_비버톡_B2B숙제/10_다국어_앱화면.md §12.7

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_summary_translation",
        sa.Column("call_summary_translation_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column("locale", sa.Text(), nullable=False, comment="번역 대상 로케일(콘솔 언어)"),
        sa.Column("text", sa.Text(), nullable=False, comment="번역된 요약"),
        sa.Column("source_locale", sa.Text(), nullable=True, comment="원본 요약의 로케일"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["call_id"], ["call.call_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("call_id", "locale", name="uq_call_summary_translation"),
    )
    op.create_index("ix_call_summary_translation_call_id", "call_summary_translation", ["call_id"])


def downgrade() -> None:
    op.drop_table("call_summary_translation")
