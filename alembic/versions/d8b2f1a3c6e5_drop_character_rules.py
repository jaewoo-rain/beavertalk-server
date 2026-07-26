"""drop character.rules — 캐릭터별 추가 규칙/금기 컬럼 제거

통화 프롬프트에서 캐릭터 rules 가 전역 불변식 템플릿과 중복·충돌이라 아예 받지
않는 형태로 전환한다(rules 에만 있던 '메타·AI 언급 금지'는 전역 템플릿으로 승격).
role/personality 는 유지. **파괴적** — 기존 rules 값은 삭제된다(downgrade 는 컬럼만 복원, 값 미복원).

Revision ID: d8b2f1a3c6e5
Revises: c7a1e4d2b9f3
Create Date: 2026-07-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8b2f1a3c6e5'
down_revision: Union[str, Sequence[str], None] = 'c7a1e4d2b9f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("character", "rules")


def downgrade() -> None:
    op.add_column(
        "character",
        sa.Column("rules", sa.Text(), nullable=True, comment="캐릭터별 추가 규칙/금기"),
    )
