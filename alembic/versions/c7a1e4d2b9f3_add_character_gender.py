"""add character.gender — 캐릭터 성별 느낌(male/female) 컬럼 추가

캐릭터에 성별 느낌(gender)을 담을 nullable Text 컬럼을 더한다.
**가법·비파괴** — 기존 행은 NULL 로 남고, 하위호환(기존 동작 불변).

Revision ID: c7a1e4d2b9f3
Revises: b3d9f0c21e47
Create Date: 2026-07-24 20:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7a1e4d2b9f3'
down_revision: Union[str, Sequence[str], None] = 'b3d9f0c21e47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "character",
        sa.Column("gender", sa.Text(), nullable=True, comment="캐릭터 성별 느낌(male/female)"),
    )


def downgrade() -> None:
    op.drop_column("character", "gender")
