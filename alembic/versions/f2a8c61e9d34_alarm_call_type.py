"""알람별 통화 모드 — alarm.call_type 추가 (프론트 요청 #1, 2026-09-23)

FCM 페이로드에 알람 id 가 없어 앱은 알람별 모드를 기기에 저장해도 어느 알람의 통화인지
되짚을 수 없다. 서버가 이미 inbound_call_id → push_dispatch_log → alarm 경로를 갖고
있으므로 여기 저장한다. auto(학습) · chat(자유대화). 기존 행은 server_default 로 auto.

Revision ID: f2a8c61e9d34
Revises: d3f7b1c92a4e
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "f2a8c61e9d34"
down_revision = "d3f7b1c92a4e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alarm",
        sa.Column(
            "call_type", sa.Text(), nullable=False, server_default="auto",
            comment="알람 통화 종류 — auto(학습) · chat(자유대화)",
        ),
    )


def downgrade() -> None:
    op.drop_column("alarm", "call_type")
