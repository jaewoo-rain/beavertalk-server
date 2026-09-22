"""통화 종류 개명 — normal → chat (C3, D3)

D3(2026-09-22): 통화 종류는 학습(auto)·자유대화(chat) 둘이다. 옛 "normal"(일반 통화)은
서버 라우팅에서 이미 "chat"으로 흡수됐다(call_session.py) — 이 마이그레이션은 **기존
데이터**를 같은 이름으로 전환해, call_type='chat' 하나만 보는 쿼리(발음 이력·일일
한도·daily-status)가 옛 통화도 빠짐없이 센다.

⛔ downgrade 는 **손실이 있다** — 전환 전에 이미 "chat"이었던 행(있었을 리 없다, 이
값은 이 배포 전엔 존재하지 않았다)과 "normal"에서 전환된 행을 구분할 수 없다. 되돌리면
전부 "normal"로 돌아간다.

Revision ID: e0a404f9e6c0
Revises: b068a7dedfd3
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "e0a404f9e6c0"
down_revision = "b068a7dedfd3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE call SET call_type = 'chat' WHERE call_type = 'normal'")
    op.alter_column(
        "call", "call_type",
        existing_type=sa.Text(), existing_nullable=False,
        server_default="chat",
        comment="통화 종류(chat/level_test/expression/freetalk — 옛 normal 은 chat 으로 전환됨)",
    )


def downgrade() -> None:
    # ⚠ 손실 있는 되돌리기 — 이 배포 이후 새로 생긴 진짜 chat 통화도 전부 normal 로
    #   돌아간다(전환된 것과 구분할 수 없다). 되돌리기 전 필요하면 call_id 목록을 따로
    #   덤프해 둬라.
    op.execute("UPDATE call SET call_type = 'normal' WHERE call_type = 'chat'")
    op.alter_column(
        "call", "call_type",
        existing_type=sa.Text(), existing_nullable=False,
        server_default="normal",
        comment="통화 종류(normal/level_test/expression/freetalk)",
    )
