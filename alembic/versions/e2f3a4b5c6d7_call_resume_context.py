"""call.resume_context — 다음 조각이 쓸 요약 슬롯

⭐ 왜 컬럼인가: 이어하기 브리프의 재료를 **조각이 끝날 때 미리** 만들어 둔다.
  이어하기 시점에 LLM 을 돌리면 "이어서" 를 누른 사용자가 그만큼 기다린다.
  통화후 분석이 어차피 조각 끝에 돌므로 거기 얹으면 왕복이 늘지 않는다.

⛔ 저장하는 것은 **원문 전사가 아니라 슬롯 JSON** 이다(topic · learner_facts · pending).
  원문을 그대로 넘기면 비버가 그 안에서 사실을 다시 찾아야 하고, 길수록 못 찾는다.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
"""

from alembic import op
import sqlalchemy as sa

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "call",
        sa.Column(
            "resume_context", sa.Text(), nullable=True,
            comment="다음 조각용 요약 슬롯(JSON) — 이어하기 브리프 재료",
        ),
    )


def downgrade() -> None:
    op.drop_column("call", "resume_context")
