"""member.name + onboarding_completed 컬럼 추가 (온보딩)

Revision ID: a7b8c9d0e1f2
Revises: f3a9c1d2b4e5
Create Date: 2026-06-24 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, Sequence[str], None] = 'f3a9c1d2b4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'member',
        sa.Column('name', sa.Text(), nullable=True, comment='이름(온보딩에서 입력)'),
    )
    op.add_column(
        'member',
        sa.Column('onboarding_completed', sa.Boolean(), server_default=sa.text('false'),
                  nullable=False, comment='온보딩(이름·학습이유·언어) 완료 여부'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('member', 'onboarding_completed')
    op.drop_column('member', 'name')
