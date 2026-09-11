"""cur_* 2단계 — cur_member_item.seen_count · cur_call.recorded_at (ADD COLUMN 둘)

계획 docs/plans/2026-09-12-cur-2단계-통화경로-이전.md §6 ②·§7 P1-7.
  · seen_count  — 예문 회전 근거(«몇 번째 보는지», §11: 첫 드릴 = 예문1, 첫 복습 = 예문2 …) + 복습 정렬 키(P1-2 — drilled_at 은 단조라
                  «마지막으로 본 순서» 를 대신할 수 없다). 목록에 실린 항목마다 통화당 +1.
  · recorded_at — 통화 종료 저장의 멱등 키(§6 ②). record_expression 은 이 값이 NULL 일 때만 쓰고 채운다 —
                  재분석·중복 종료·조각 재개가 카운터(seen_count·expression_calls·quiz_failed_count)를 두 번 올리지 못하게.
⚠ a1c2d3e4f5b6 을 고쳐 재적용하지 않고 **새 리비전**으로 간다(P1-7 — 다른 PC·하네스 서버의 옛 파일과 어긋난다). 되돌리기 = DROP COLUMN.
⛔ 워크트리에서 upgrade 를 돌리지 않는다 — 운영 DB 적용은 bt-back 이 «적용» 뒤 한다.

Revision ID: b2d3e4f5a6c7
Revises: a1c2d3e4f5b6
Create Date: 2026-09-12
"""
from alembic import op
import sqlalchemy as sa

revision = "b2d3e4f5a6c7"
down_revision = "a1c2d3e4f5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cur_member_item",
        sa.Column("seen_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False,
                  comment="목록에 실린 횟수 — 예문 회전·복습 정렬 키"),
    )
    op.add_column(
        "cur_call",
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True,
                  comment="종료 저장 완료 시각 — NULL 일 때만 record 가 쓴다(멱등 키)"),
    )


def downgrade() -> None:
    op.drop_column("cur_call", "recorded_at")
    op.drop_column("cur_member_item", "seen_count")
