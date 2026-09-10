# -*- coding: utf-8 -*-
"""member_item_progress — 표현학습 진도 3컬럼(quiz_passed_at · drilled_at · drilled_call_id)

⭐ **새 테이블을 안 만든다.** `member_item_progress` 는 이미 회원×항목 **1행**이라
  조각·통화·날짜와 무관하게 진도가 이어진다 — 사장님 요구(«다음날 통화할 때 이어서»)가
  컬럼 3개로 공짜가 된다.

⛔ **노출 «횟수» 컬럼을 만들지 않았다**(사장님 정정 2026-09-10). 기준이 3회가 아니라
  **1회**다 ⇒ 재는 것이 «몇 번» 이 아니라 «했나/안 했나» 이고 timestamp 하나로 끝난다.
  카운터를 두면 연속 접기 알고리즘까지 딸려 온다.

⚠ 완료 판정의 **유일한** 기준은 `quiz_passed_at` 이다. `drilled_at` 은 선별 정렬용
  (드릴했는데 못 끝낸 것을 다음 통화 앞으로) — 두 기준을 만들지 마라.

⚠ `drilled_call_id` 는 기존 `first_call_id`/`last_call_id` 와 **같은 SET NULL 규약**이다.
  통화가 지워져도 진도는 남아야 한다(진도는 통화의 부속물이 아니다).

설계: docs/plans/2026-09-10-표현학습-프리토킹-통화-분리.md §2-3
구현: docs/20260910_0230_표현학습-프리토킹-구현계획.md §4

Revision ID: 9c4e17a2f8b3
Revises: e2f3a4b5c6d7
"""

from alembic import op
import sqlalchemy as sa

revision = "9c4e17a2f8b3"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "member_item_progress",
        sa.Column(
            "quiz_passed_at", sa.DateTime(timezone=True), nullable=True,
            comment="표현학습 퀴즈 통과 시각(NULL=미통과) — 완료 판정의 유일한 기준",
        ),
    )
    op.add_column(
        "member_item_progress",
        sa.Column(
            "drilled_at", sa.DateTime(timezone=True), nullable=True,
            comment="표현학습에서 마지막으로 드릴한 시각(NULL=한 번도 안 꺼냄)",
        ),
    )
    op.add_column(
        "member_item_progress",
        sa.Column(
            "drilled_call_id", sa.BigInteger(), nullable=True,
            comment="마지막으로 드릴한 통화(되짚기용)",
        ),
    )
    op.create_foreign_key(
        "fk_mip_drilled_call", "member_item_progress", "call",
        ["drilled_call_id"], ["call_id"], ondelete="SET NULL",
    )
    # 선별이 «이 회원의 아직 통과 못 한 행» 을 고른다.
    op.create_index(
        "ix_mip_member_quiz", "member_item_progress", ["member_id", "quiz_passed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mip_member_quiz", table_name="member_item_progress")
    op.drop_constraint("fk_mip_drilled_call", "member_item_progress", type_="foreignkey")
    op.drop_column("member_item_progress", "drilled_call_id")
    op.drop_column("member_item_progress", "drilled_at")
    op.drop_column("member_item_progress", "quiz_passed_at")
