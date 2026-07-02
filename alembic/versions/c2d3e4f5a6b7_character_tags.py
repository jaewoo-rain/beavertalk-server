"""character tags (음색/특성 태그)

캐릭터 카드/시트에 표시할 음색·특성 태그(예: Warm, Calm, Soft)를 저장할 JSON
배열 컬럼을 character 에 추가한다. NULL=태그 없음(서비스에서 [] 로 변환).

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-07-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, Sequence[str], None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'character',
        sa.Column(
            'tags',
            sa.JSON(),
            nullable=True,
            comment='음색/특성 태그 배열(예: Warm, Calm, Soft)',
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('character', 'tags')
