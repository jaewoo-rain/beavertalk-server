"""add_call_pron_feedback_and_review_counted (Rev1 — add-only, 비파괴)

통화후 발음·피드백 확장의 저장 계층 1/2.

- call.feedback / pron_feedback / pron_feedback_n: 전부 NULL 허용 → 백필 불필요, 무중단.
- review.counted: NOT NULL + server_default text('true') → 기존 행은 자동으로 true 로
  백필(과거 복습은 전부 산입 대상). 이후 apply_score=false 복습만 false 로 들어온다.

nationality_prediction 테이블은 후속 Rev2(create_nationality_prediction)에서 생성한다.

downgrade 는 add-only 라 컬럼만 제거 — 신규 데이터 유실은 있으나 파생/부가 컬럼.

Revision ID: 32cc30f95a44
Revises: c9d0e1f2a3b4
Create Date: 2026-07-16 18:03:12.213480

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '32cc30f95a44'
down_revision: Union[str, Sequence[str], None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — call 3컬럼 + review.counted(백필 true)."""
    op.add_column(
        'call',
        sa.Column('feedback', sa.Text(), nullable=True,
                  comment='통화 코칭 한 문장(통화후 분석 생성)'),
    )
    op.add_column(
        'call',
        sa.Column('pron_feedback', sa.Text(), nullable=True,
                  comment='발음 LLM 한마디 캐시'),
    )
    op.add_column(
        'call',
        sa.Column('pron_feedback_n', sa.Integer(), nullable=True,
                  comment='발음 한마디 캐시 무효화키(생성 시점 counted 복습 수)'),
    )
    op.add_column(
        'review',
        sa.Column('counted', sa.Boolean(), server_default=sa.text('true'), nullable=False,
                  comment='이 리뷰가 문장 공식점수(Evaluation)·소리집계에 산입되는가'),
    )


def downgrade() -> None:
    """Downgrade schema — add-only 컬럼 제거."""
    op.drop_column('review', 'counted')
    op.drop_column('call', 'pron_feedback_n')
    op.drop_column('call', 'pron_feedback')
    op.drop_column('call', 'feedback')
