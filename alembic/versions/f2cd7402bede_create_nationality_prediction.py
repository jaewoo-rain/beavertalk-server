"""create_nationality_prediction (Rev2 — add-only, 비파괴)

통화후 발음·피드백 확장의 저장 계층 2/2. 국적 예측 이력 테이블.

- 매 통화 user 음성 → 외부 국적 API 예측을 append. 회원별 최근 N개(FIFO) 평균으로
  SpeakCountry(억양) 재계산의 원본.
- predictions: JSON(JSONB 아님 — sqlite 테스트 호환), NOT NULL.
- member_id FK CASCADE(회원 삭제 시 이력 제거), call_id FK SET NULL(통화 삭제 시 이력 보존).
- UNIQUE(call_id)=uq_ncp_call: 통화당 예측 1회(멱등). NULL 다중 허용(Postgres 기본).
- ix_ncp_member_created(member_id, created_at): 회원별 최근 N개 FIFO 조회 커버.
- ix_nationality_prediction_member_id: member_id 단일(FK 조인). 모델의 index=True 유래.

downgrade 는 테이블·데이터 전체 유실 — 신규 테이블이라 prod 백필 불필요(무해).

Revision ID: f2cd7402bede
Revises: 32cc30f95a44
Create Date: 2026-07-16 18:03:53.060201

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2cd7402bede'
down_revision: Union[str, Sequence[str], None] = '32cc30f95a44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — nationality_prediction 테이블 생성."""
    op.create_table(
        'nationality_prediction',
        sa.Column('prediction_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('member_id', sa.BigInteger(), nullable=False, comment='회원'),
        sa.Column('call_id', sa.BigInteger(), nullable=True,
                  comment='예측을 유발한 통화(통화 삭제 시 이력 보존 — SET NULL)'),
        sa.Column('predictions', sa.JSON(), nullable=False,
                  comment='[{country,iso,prob}] — 외부 국적 API 원응답 예측 배열'),
        sa.Column('top1', sa.Text(), nullable=True,
                  comment='최상위 예측 국가명(predictions[0] 파생 캐시)'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False, comment='생성 시각'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False, comment='수정 시각'),
        sa.ForeignKeyConstraint(['member_id'], ['member.member_id'],
                                name='fk_ncp_member', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['call_id'], ['call.call_id'],
                                name='fk_ncp_call', ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('prediction_id'),
        sa.UniqueConstraint('call_id', name='uq_ncp_call'),
    )
    op.create_index(op.f('ix_nationality_prediction_member_id'),
                    'nationality_prediction', ['member_id'], unique=False)
    op.create_index('ix_ncp_member_created', 'nationality_prediction',
                    ['member_id', 'created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema — 테이블 삭제(신규 테이블 — 무해)."""
    op.drop_index('ix_ncp_member_created', table_name='nationality_prediction')
    op.drop_index(op.f('ix_nationality_prediction_member_id'), table_name='nationality_prediction')
    op.drop_table('nationality_prediction')
