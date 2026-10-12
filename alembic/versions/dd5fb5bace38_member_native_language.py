"""member.native_language — 회원이 고른 모국어(ISO 639-1)

Revision ID: dd5fb5bace38
Revises: 7c3e9a1d5b20
Create Date: 2026-10-12 15:30:00

PM-DEC-513·514(모국어 칸 분리 · F066). 앱 언어(language)와 별개 열이다 —
PATCH language 는 이 칸을 건드리지 않는다. NULL 허용·기본값·백필 없음(R4 A안 가정) —
NULL 은 「기록 없음」이고 B2B 서버는 그때 language 로 대체해 읽는다(mirrors.py).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "dd5fb5bace38"
down_revision: Union[str, None] = "7c3e9a1d5b20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "member",
        sa.Column(
            "native_language", sa.Text(), nullable=True,
            comment="회원이 고른 모국어(ISO 639-1). NULL=기록 없음(language 로 대체)",
        ),
    )


def downgrade() -> None:
    op.drop_column("member", "native_language")
