"""curriculum_conversation_goal — 챕터별 회화 목표(AI 일괄 정리 결과 · 원본과 분리 저장)

Revision ID: 399820b3abe2
Revises: dd5fb5bace38
Create Date: 2026-10-13 22:00:00

PM-DEC-553·559·565. 회화 목표를 learning_item 원본과 분리해 둔다 — 비우면 원래
방식(priority_rank)으로 돌아간다. 이 마이그레이션은 **빈 테이블만** 만든다(적재는 별도 ·
55 커리큘럼데이터 하네스 08_적재계획). 읽기는 b2b 가 한다(목표는 b2b 재료로 내려온다).

- goal_rank 0 · item_id NULL 행 = 「회화 목표 없음」 챕터 표시(접사만 있는 챕터 · PM-DEC-559 D02).
- item_id 는 learning_item 을 가리키며 원본 행 삭제를 막는다(ON DELETE RESTRICT).
- downgrade 는 테이블만 지운다(원본 무변).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "399820b3abe2"
down_revision: Union[str, None] = "dd5fb5bace38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "curriculum_conversation_goal",
        sa.Column("language", sa.Text(), nullable=False, comment="학습 언어(ko)"),
        sa.Column("topik_grade", sa.SmallInteger(), nullable=False, comment="급수(1~6)"),
        sa.Column("chapter_no", sa.SmallInteger(), nullable=False, comment="챕터 번호"),
        sa.Column("goal_rank", sa.SmallInteger(), nullable=False,
                  comment="목표 순서(1부터) · 0 = 이 챕터는 회화 목표 없음"),
        sa.Column("item_id", sa.BigInteger(),
                  sa.ForeignKey("learning_item.item_id", ondelete="RESTRICT"), nullable=True,
                  comment="learning_item.item_id · goal_rank 0 이면 NULL"),
        sa.Column("reason", sa.Text(), nullable=True, comment="고른 이유(AI 산출 · 검수용)"),
        sa.Column("batch_id", sa.Text(), nullable=True, comment="일괄 실행 id"),
        sa.Column("model", sa.Text(), nullable=True, comment="고른 모델"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("language", "topik_grade", "chapter_no", "goal_rank",
                                name="curriculum_conversation_goal_pkey"),
        sa.CheckConstraint(
            "(goal_rank = 0 AND item_id IS NULL) OR (goal_rank >= 1 AND item_id IS NOT NULL)",
            name="curriculum_conversation_goal_rank_item_check",
        ),
        comment="챕터별 회화 목표(원본 분리 저장 · 비우면 priority_rank 방식) — PM-DEC-553",
    )
    op.create_index("ix_curriculum_conversation_goal_item_id", "curriculum_conversation_goal", ["item_id"])
    # 운영 public 테이블 69개가 모두 RLS 켜짐(2026-10-13 실측) — 정책 없이 켜서 Supabase 공개 API 를
    # 막는다. 서버·b2b 는 소유자 연결이라 영향 없음.
    op.execute("ALTER TABLE curriculum_conversation_goal ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index("ix_curriculum_conversation_goal_item_id", table_name="curriculum_conversation_goal")
    op.drop_table("curriculum_conversation_goal")
