"""학습 달력용 사용자 단어 수 — call.user_word_count 추가 (C11)

docs/plans/2026-09-22-프리미엄-자유대화-15분-달력.md 의 C11. total_time 과 같은
방식으로 조각마다 누적. 전사가 없으면 NULL(0 과 «집계 없음» 을 가른다).

Revision ID: d3f7b1c92a4e
Revises: c1a2f9e4d0b7
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "d3f7b1c92a4e"
down_revision = "c1a2f9e4d0b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "call",
        sa.Column(
            "user_word_count", sa.Integer(), nullable=True,
            comment="사용자 발화 단어 수(ja·zh 는 글자수/2) — NULL=집계 없음(전사 없음)",
        ),
    )


def downgrade() -> None:
    op.drop_column("call", "user_word_count")
