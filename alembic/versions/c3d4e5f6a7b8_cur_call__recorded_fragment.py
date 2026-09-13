"""cur_* 2단계 — cur_call.recorded_fragment (ADD COLUMN) — 종료 저장 멱등 단위를 «통화» 에서 «조각» 으로

실통화 1550(5분×3조각, 2026-09-12): 조각 1 종료가 recorded_at 을 찍자 §6 ② 멱등 키가 조각 2·3 의 record_expression 을 막았다
(«이미 저장됨 … no-op») — 조각 3 에서 통과한 3개가 cur_member_item 에 없다. §6 ② 멱등과 §7 P1-5 조각 병합이 충돌한 것.
  · recorded_fragment — 마지막으로 저장한 조각 번호(call.fragment_count 와 같은 축, 0 = 아직 없음). record_expression 은
    fragment_no > recorded_fragment 일 때만 쓰고 이 값을 fragment_no 로 올린다 — 같은 조각의 중복 종료·재분석은 여전히 no-op.
  · recorded_at 은 «처음 저장 시각» 으로 남는다(의미 변경 — 더는 멱등 키가 아니다).
⚠ 새 리비전(P1-7). 되돌리기 = DROP COLUMN. ⛔ 워크트리에서 upgrade 를 돌리지 않는다 — 운영 DB 적용은 bt-back 이 «적용» 뒤 한다.

Revision ID: c3d4e5f6a7b8
Revises: b2d3e4f5a6c7
Create Date: 2026-09-13
"""
from alembic import op
import sqlalchemy as sa

revision = "c3d4e5f6a7b8"
down_revision = "b2d3e4f5a6c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cur_call",
        sa.Column("recorded_fragment", sa.SmallInteger(), server_default=sa.text("0"), nullable=False,
                  comment="마지막으로 종료 저장한 조각 번호(call.fragment_count 축, 0=없음) — 조각 단위 멱등 키"),
    )


def downgrade() -> None:
    op.drop_column("cur_call", "recorded_fragment")
