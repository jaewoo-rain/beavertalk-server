"""과제에 워크북 PDF 외부 링크 칸을 붙인다.

워크북 과제는 앱 안에서 파일을 열지 않는다 — 교사가 올린 PDF 의 외부 링크를
그대로 보관하고 앱은 브라우저로 넘긴다. 뷰어를 앱에 들이면 30 로케일 폰트가
따라오고, 서버가 파일을 보관하면 저작권 주체가 우리로 바뀐다.

이 칸이 없으면 앱의 「다운로드」 버튼이 열 곳이 없어 비활성으로 남는다.

설계: 23_제품기획_하네스/_output/2026-08-19_비버톡_B2B숙제/13_앱숙제_구현계획.md §2.7

Revision ID: f1a2b3c4d5e6
Revises: e1f2a3b4c5d6
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f1a2b3c4d5e6"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assignment",
        sa.Column(
            "workbook_url",
            sa.Text(),
            nullable=True,
            comment="워크북 PDF 외부 링크(교사가 입력)",
        ),
    )


def downgrade() -> None:
    op.drop_column("assignment", "workbook_url")
