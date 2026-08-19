"""call.fragment_count — 이어하기 조각 수

⭐ 왜 새 테이블·부모 id 가 아니라 컬럼 하나인가(2026-08-19 사장님 결정):
  조각을 **새 통화 행으로 만들지 않는다.** 같은 행에 계속 쓴다.
  그러면 통화 목록·통화후 분석·발음 점수·일일 한도가 전부 `call_id` 기준이라
  **묶을 게 없다** — 15분 대화가 저절로 1건이다.
  (설계 초안의 `root_call_id` 로 3건을 묶는 방식보다 싸고, 놓칠 자리가 적다.)

⚠ 그래서 이 컬럼이 하는 일은 "몇 번 이었나" 하나뿐이다. 조각 상한(Free 1 / Pro·Max 3)
  판정과 관측에 쓴다.

⛔ 기존 행은 **1** 로 채운다(0 이 아니다). 이어하기 없는 통화도 조각 1개다 —
  0 으로 두면 "상한 3" 판정에서 한 번을 더 주게 된다.

Revision ID: d1e2f3a4b5c6
Revises: c8f3a2b1d704
"""

from alembic import op
import sqlalchemy as sa

revision = "d1e2f3a4b5c6"
down_revision = "c8f3a2b1d704"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ⚠ server_default 를 준다 — 안 주면 기존 행에 NOT NULL 을 못 건다.
    op.add_column(
        "call",
        sa.Column(
            "fragment_count", sa.Integer(), nullable=False, server_default=sa.text("1"),
            comment="이어하기 조각 수(1=이어하기 없음)",
        ),
    )


def downgrade() -> None:
    op.drop_column("call", "fragment_count")
