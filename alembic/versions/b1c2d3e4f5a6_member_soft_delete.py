"""member soft delete (deleted_at)

회원 탈퇴를 하드 삭제 → 소프트 삭제로 전환. member 에 deleted_at 컬럼을 추가한다
(NULL=활성, 값=탈퇴 시각). 탈퇴 시 email·auth_user_id 는 서비스에서 NULL 로 비워
같은 이메일 재가입을 허용한다.

Revision ID: b1c2d3e4f5a6
Revises: 8390e67d05bd
Create Date: 2026-07-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = '8390e67d05bd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'member',
        sa.Column(
            'deleted_at',
            sa.DateTime(timezone=True),
            nullable=True,
            comment='탈퇴 시각(소프트 삭제, NULL=활성)',
        ),
    )
    op.create_index(
        op.f('ix_member_deleted_at'), 'member', ['deleted_at'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_member_deleted_at'), table_name='member')
    op.drop_column('member', 'deleted_at')
