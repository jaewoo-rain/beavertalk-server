"""call_raw_data 에 role(화자) + turn_index(턴 순서) 추가

통화후 분석이 전사를 USER/BEAVER 로 복원하고 턴 순서를 유지하려면 화자/순서가 필요.
둘 다 nullable → 무중단.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-06-25 08:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, Sequence[str], None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('call_raw_data', sa.Column('role', sa.Text(), nullable=True, comment='화자(user/beaver)'))
    op.add_column('call_raw_data', sa.Column('turn_index', sa.Integer(), nullable=True, comment='턴 순서(0부터)'))


def downgrade() -> None:
    op.drop_column('call_raw_data', 'turn_index')
    op.drop_column('call_raw_data', 'role')
