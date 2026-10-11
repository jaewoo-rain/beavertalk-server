"""member.actual_nationality — 회원이 고른 실제 국적(ISO 3166-1 alpha-2)

Revision ID: 7c3e9a1d5b20
Revises: d2e3f4051607
Create Date: 2026-10-10 22:30:00

PM-DEC-495·498(국적 선택). 모국어(language)·억양 결과(speak_country_id)와 별개 열이다.
NULL 허용·기본값·백필 없음 — NULL 은 「고르지 않음」이다.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7c3e9a1d5b20"
down_revision: Union[str, None] = "d2e3f4051607"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "member",
        sa.Column(
            "actual_nationality", sa.Text(), nullable=True,
            comment="회원이 고른 실제 국적(ISO 3166-1 alpha-2). NULL=미선택",
        ),
    )


def downgrade() -> None:
    op.drop_column("member", "actual_nationality")
