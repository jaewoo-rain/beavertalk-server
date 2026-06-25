"""정리: review.record_time + member.interests/example_sentences 컬럼 제거

- record_time: created_at 과 중복 → 제거
- interests: 흥미는 member_reason(온보딩 학습이유)에서 가져옴 → 별도 컬럼 제거
- example_sentences: 어디서도 미사용(죽은 컬럼) → 제거

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-06-25 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3e4f5a6b7c8'
down_revision: Union[str, Sequence[str], None] = 'c2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('review', 'record_time')
    op.drop_column('member', 'example_sentences')
    op.drop_column('member', 'interests')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('member', sa.Column('interests', sa.Text(), nullable=True, comment='관심사(콤마구분 코드)'))
    op.add_column('member', sa.Column('example_sentences', sa.Text(), nullable=True, comment='통화 프롬프트용 예시 문장(개행 구분)'))
    op.add_column('review', sa.Column('record_time', sa.DateTime(timezone=True), nullable=True, comment='기록 시간'))
