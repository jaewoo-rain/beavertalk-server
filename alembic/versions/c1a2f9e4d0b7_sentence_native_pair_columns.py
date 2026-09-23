"""현지인 표현 짝 문장 — sentence 에 kind·paired_sentence_id·nuance 추가 (C9)

docs/plans/2026-09-22-프리미엄-자유대화-15분-달력.md 의 C9. 기본 문장은 세 칸 전부
NULL — kind='native' 인 행만 값을 가지며 paired_sentence_id 로 기본 문장을 가리킨다.
같은 테이블에 "짝" 개념을 얹은 것이지 새 종류의 문장이 아니다(sentence.py 모델 주석 참조).

Revision ID: c1a2f9e4d0b7
Revises: 9ca691f752eb
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "c1a2f9e4d0b7"
down_revision = "9ca691f752eb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sentence",
        sa.Column("kind", sa.Text(), nullable=True, comment="NULL=기본 문장 · 'native'=현지인 표현 짝"),
    )
    op.add_column(
        "sentence",
        sa.Column(
            "paired_sentence_id", sa.BigInteger(), nullable=True,
            comment="kind='native' 인 행이 가리키는 기본 문장(NULL=기본 문장 자신)",
        ),
    )
    op.add_column(
        "sentence",
        sa.Column("nuance", sa.Text(), nullable=True, comment="현지인 표현의 뉘앙스 한 줄(모국어, kind='native' 에만 값)"),
    )
    op.create_foreign_key(
        "fk_sentence_paired", "sentence", "sentence",
        ["paired_sentence_id"], ["sentence_id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_sentence_paired", "sentence", type_="foreignkey")
    op.drop_column("sentence", "nuance")
    op.drop_column("sentence", "paired_sentence_id")
    op.drop_column("sentence", "kind")
