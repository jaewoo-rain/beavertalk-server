"""통화 예산/이어하기 전용 시각 2종 — call.fragment_started_at · fragment_ended_at (C4 재검-①②)

QA C4 재검(2026-09-23):
①(높음) 이어하기 TTL 판정이 `updated_at` 을 쓰면 통화 종료 "후행" 쓰기(분석·usage 기록·
  TTS·평점 PATCH 등)가 전부 그 값을 밀어, TTL 이 실제 조각 종료 시각보다 계속 늘어난다.
  ⇒ `fragment_ended_at` — `finalize_call`(조각마다 부른다)이 찍는 **조각 종료 전용** 시각.
②(높음) 이어하기 조각이 진행 중일 때 예산 합산이 그 조각의 경과 시간을 0으로 센다(앞
  조각의 `total_time` 이 이미 non-NULL 이라 estimate 로직이 안 붙는다).
  ⇒ `fragment_started_at` — 새 통화 생성·이어하기 재개 때 찍는 **이번 조각 시작** 시각.
  예산 집계(`sum_total_time_in_window`)가 `status='ongoing'` 인 행에서 이 값으로부터의
  경과를 더한다(ABSOLUTE_CALL_TIMEOUT_S=540 상한).

둘 다 NULL 허용 — 기존 행은 NULL 이고, 소비처가 그 경우 `updated_at` 으로 폴백한다
(옛 행 호환, 백필 불필요).

Revision ID: 04566cafd21b
Revises: e0a404f9e6c0
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "04566cafd21b"
down_revision = "e0a404f9e6c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "call",
        sa.Column(
            "fragment_started_at", sa.DateTime(timezone=True), nullable=True,
            comment="이번 조각이 시작된 시각(새 통화 생성·이어하기 재개 때 찍음) — 예산의 ongoing 경과 추정 기준",
        ),
    )
    op.add_column(
        "call",
        sa.Column(
            "fragment_ended_at", sa.DateTime(timezone=True), nullable=True,
            comment="가장 최근 조각이 끝난 시각(finalize_call 이 조각마다 찍음) — 이어하기 TTL 기준. NULL=옛 행(updated_at 폴백)",
        ),
    )


def downgrade() -> None:
    op.drop_column("call", "fragment_ended_at")
    op.drop_column("call", "fragment_started_at")
