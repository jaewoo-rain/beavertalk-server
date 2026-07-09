"""add push device_token, push_dispatch_log

예약전화 FCM 발송용 두 테이블 추가.
- device_token : 회원 기기 푸시 토큰(전역 UNIQUE, member CASCADE, is_valid soft-invalidate)
- push_dispatch_log : (alarm, 의도된 벽분) UNIQUE 멱등 클레임 — 중복 발송 방지

둘 다 신규 테이블(기존 테이블 ALTER 없음) → 무중단·비파괴적.

Revision ID: d4e5f6a7b8c9
Revises: c2d3e4f5a6b7
Create Date: 2026-07-06 02:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'device_token',
        sa.Column('device_token_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('member_id', sa.BigInteger(), nullable=False, comment='회원'),
        sa.Column('platform', sa.Text(), nullable=False, comment='android_fcm | ios_voip'),
        sa.Column('token', sa.Text(), nullable=False, comment='푸시 토큰'),
        sa.Column('is_valid', sa.Boolean(), server_default=sa.text('true'), nullable=False, comment='유효 여부(발송 폐기 시 False)'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='생성 시각'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='수정 시각'),
        sa.ForeignKeyConstraint(['member_id'], ['member.member_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('device_token_id'),
    )
    op.create_index(op.f('ix_device_token_member_id'), 'device_token', ['member_id'], unique=False)
    op.create_index(op.f('ix_device_token_token'), 'device_token', ['token'], unique=True)

    op.create_table(
        'push_dispatch_log',
        sa.Column('push_dispatch_log_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('alarm_id', sa.BigInteger(), nullable=False, comment='알람'),
        sa.Column('intended_fire_minute', sa.Text(), nullable=False, comment='의도된 벽분 버킷, 예 2026-07-06 08:00'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='생성 시각'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='수정 시각'),
        sa.ForeignKeyConstraint(['alarm_id'], ['alarm.alarm_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('push_dispatch_log_id'),
        sa.UniqueConstraint('alarm_id', 'intended_fire_minute', name='uq_push_dispatch_alarm_minute'),
    )
    op.create_index(op.f('ix_push_dispatch_log_alarm_id'), 'push_dispatch_log', ['alarm_id'], unique=False)
    op.create_index('ix_push_dispatch_log_created_at', 'push_dispatch_log', ['created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_push_dispatch_log_created_at', table_name='push_dispatch_log')
    op.drop_index(op.f('ix_push_dispatch_log_alarm_id'), table_name='push_dispatch_log')
    op.drop_table('push_dispatch_log')
    op.drop_index(op.f('ix_device_token_token'), table_name='device_token')
    op.drop_index(op.f('ix_device_token_member_id'), table_name='device_token')
    op.drop_table('device_token')
