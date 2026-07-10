"""call 레벨테스트 컬럼 3개 — call_type / assessed_level / assessment_note

레벨테스트 콜(P1)이 통화 종류와 판정 결과를 call 행에 귀속 저장한다.
domains/learning/models/call.py 확장과 1:1.

- call_type: TEXT NOT NULL server_default 'normal' — 기존 행 전부 'normal' 백필
  (server_default 로 무중단 추가, NOT NULL 안전). CHECK 없음 — call.status 컨벤션
  (TEXT + server_default, 값 화이트리스트는 앱 계층) 과 동일.
- assessed_level / assessment_note: NULL 허용(level_test 전용) → 백필 불필요.
- ALTER 만 3건(신규 컬럼 추가) → 무중단·비파괴적. downgrade 는 컬럼 3개 drop
  (판정 데이터 유실 — 적용 후 되돌림은 파괴적).

설계: docs/20260709_1231_level-system-master-plan.md §3.3 call 확장.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-09 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, Sequence[str], None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'call',
        sa.Column(
            'call_type',
            sa.Text(),
            server_default=sa.text("'normal'"),
            nullable=False,
            comment='통화 종류(normal/level_test)',
        ),
    )
    op.add_column(
        'call',
        sa.Column(
            'assessed_level',
            sa.Integer(),
            nullable=True,
            comment='레벨테스트 판정 결과(1~13, level_test 전용)',
        ),
    )
    op.add_column(
        'call',
        sa.Column(
            'assessment_note',
            sa.Text(),
            nullable=True,
            comment='판정 근거(모델 reasoning — 감사·디버깅용)',
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('call', 'assessment_note')
    op.drop_column('call', 'assessed_level')
    op.drop_column('call', 'call_type')
