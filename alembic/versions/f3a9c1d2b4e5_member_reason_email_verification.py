"""member_reason + email_verification 테이블 추가

학습 이유(1:N) + 이메일 인증 코드(회원가입/비번재설정 공용) 저장소.

Revision ID: f3a9c1d2b4e5
Revises: 13b1caa5b707
Create Date: 2026-06-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a9c1d2b4e5'
down_revision: Union[str, Sequence[str], None] = '13b1caa5b707'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'member_reason',
        sa.Column('member_reason_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('member_id', sa.BigInteger(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False, comment='학습 이유 코드'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='생성 시각'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='수정 시각'),
        sa.ForeignKeyConstraint(['member_id'], ['member.member_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('member_reason_id'),
        sa.UniqueConstraint('member_id', 'reason', name='uq_member_reason'),
    )
    op.create_index(op.f('ix_member_reason_member_id'), 'member_reason', ['member_id'], unique=False)

    op.create_table(
        'email_verification',
        sa.Column('email_verification_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('email', sa.Text(), nullable=False, comment='대상 이메일'),
        sa.Column('purpose', sa.Text(), nullable=False, comment='signup | pwreset'),
        sa.Column('code_hash', sa.Text(), nullable=False, comment='코드 bcrypt 해시'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False, comment='코드 만료 시각'),
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False, comment='코드 입력 시도 횟수'),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True, comment='인증 완료 시각'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='생성 시각'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='수정 시각'),
        sa.PrimaryKeyConstraint('email_verification_id'),
        sa.UniqueConstraint('email', 'purpose', name='uq_email_verification'),
    )
    op.create_index(op.f('ix_email_verification_email'), 'email_verification', ['email'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_email_verification_email'), table_name='email_verification')
    op.drop_table('email_verification')
    op.drop_index(op.f('ix_member_reason_member_id'), table_name='member_reason')
    op.drop_table('member_reason')
