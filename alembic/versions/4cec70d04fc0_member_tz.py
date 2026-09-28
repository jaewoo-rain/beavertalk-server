# -*- coding: utf-8 -*-
"""member.tz 추가 — 알람 시간대 자동 추적(2026-09-29)

앱이 이미 보내는 IANA tz(GET /calls/daily-status?tz= · GET /stats/calendar?tz= ·
WS start.tz)를 서버가 member.tz 에 적어 두고, 예약전화 디스패치가
member.tz → alarm.tz → Asia/Seoul 순으로 존을 고른다(dispatch_service._alarm_zone).

⛔ 기본값·백필 없음 — NULL 은 "아직 모름"이다. 기존 행은 전부 NULL 로 시작하고,
   NULL 인 동안 디스패치는 지금과 **똑같이** 동작한다(alarm.tz → 서울).
   순수 컬럼 추가라 잠금·재작성 없음(nullable, default 없음).

Revision ID: 4cec70d04fc0
Revises: f737b32e72cf
"""

import sqlalchemy as sa
from alembic import op

revision = "4cec70d04fc0"
down_revision = "f737b32e72cf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "member",
        sa.Column(
            "tz", sa.Text(), nullable=True,
            comment="회원이 마지막으로 알려온 기기 IANA 시간대 — 알람이 현지 시각을 따라가게 하는 값",
        ),
    )


def downgrade() -> None:
    op.drop_column("member", "tz")
