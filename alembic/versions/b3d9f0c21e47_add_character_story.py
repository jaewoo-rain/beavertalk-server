"""add character.story — 캐릭터 스토리/서사(배경 이야기) 컬럼 추가

캐릭터 상세에 배경 이야기(story)를 담을 nullable Text 컬럼을 더한다.
**가법·비파괴** — 기존 행은 NULL 로 남고, 하위호환(기존 동작 불변).

Revision ID: b3d9f0c21e47
Revises: f10b3c05cf8e
Create Date: 2026-07-24 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3d9f0c21e47'
down_revision: Union[str, Sequence[str], None] = 'f10b3c05cf8e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "character",
        sa.Column("story", sa.Text(), nullable=True, comment="캐릭터 스토리/서사(배경 이야기)"),
    )


def downgrade() -> None:
    op.drop_column("character", "story")
