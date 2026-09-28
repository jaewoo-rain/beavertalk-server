# -*- coding: utf-8 -*-
"""alarm.tz / alarm.tz_offset_min — 알람 시간대(§5)

앱 요청서 §5, docs/plans/2026-09-28-알람시간대-해지사유-5-17.md §5. 지금 디스패치
(dispatch_service.py)는 항상 Asia/Seoul 벽시각으로 시·분·요일을 대조한다 — 뉴욕
사용자가 고른 「월 08:00」이 서울 월요일(=뉴욕 일요일 19:00)에 울리는 원인이다.

⛔ 기본값 없음 · ⛔ 기존 활성 알람 17행 백필 없음 — 그들의 실제 시간대는 서버
어디에도 없다(call 표에도 tz 칸 없음, 언어 ≠ 시간대). 추측으로 채우면 엉뚱한
시각에 울린다. 컬럼 추가 후 전부 NULL → 지금과 동일하게 서울로 동작한다(디스패치
의 폴백 체인 — 모델이 아니라 dispatch_service.py 한 곳에만 둔다). 앱이 알람을
수정할 때 새 tz 로 덮인다.

Revision ID: a958b64ce7fd
Revises: 41f8f1b63ee4
"""

from alembic import op
import sqlalchemy as sa

revision = "a958b64ce7fd"
down_revision = "41f8f1b63ee4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alarm",
        sa.Column(
            "tz", sa.Text(), nullable=True,
            comment="IANA 시간대(예: America/New_York), NULL=미상",
        ),
    )
    op.add_column(
        "alarm",
        sa.Column(
            "tz_offset_min", sa.Integer(), nullable=True,
            comment="tz 파싱 실패/부재 시 폴백 고정 오프셋(분, 동쪽 +), NULL=미상",
        ),
    )


def downgrade() -> None:
    op.drop_column("alarm", "tz_offset_min")
    op.drop_column("alarm", "tz")
