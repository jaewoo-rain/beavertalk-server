# -*- coding: utf-8 -*-
"""churn_reason 표 신설 — 해지 사유 수집(§17)

앱 요청서 §17(2026-09-26), docs/plans/2026-09-28-알람시간대-해지사유-5-17.md §17.
구독 해지 설문("왜 그만두시나요?") 응답을 담는 새 표. `subscribe` 테이블에 칸을
붙이지 않는다 — 구독 행은 결제 상태 기계이고 subscription_status 가 읽는 표다.

유니크 (member_id, subscribe_id) — 「어느 구독이 끝났나」로 응답을 식별한다(서버가
resolve_status 로 "지금 만료된 구독"을 스스로 추론하지 않는다 — 앱이 이미 들고
있는 subscribe_id 를 그대로 받고 소유만 검증한다).

두 FK 모두 nullable + ON DELETE SET NULL — 결제 3표(payment·subscribe·iap_receipt,
S2)와 같은 패턴. 해지 사유는 법적 보존 의무는 없지만, CASCADE 로 회원 삭제 때 같이
지우면 이탈 분석 자체가 사라진다. SET NULL 이면 reason·offer_shown·created_at 은
남고 사람과의 연결만 끊긴다 — 탈퇴 회원 행이 여럿이면 유니크가 (NULL, NULL) 로
전부 부딪히므로 `NULLS NOT DISTINCT`(PG 17.6)로 하나로 뭉친다.

⛔ 백필 없음 — 새 표다.

Revision ID: 41f8f1b63ee4
Revises: 96d98b7d47a5
"""

from alembic import op
import sqlalchemy as sa

revision = "41f8f1b63ee4"
down_revision = "96d98b7d47a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "churn_reason",
        sa.Column("churn_reason_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "member_id", sa.BigInteger(),
            sa.ForeignKey("member.member_id", ondelete="SET NULL"), nullable=True,
            comment="회원",
        ),
        sa.Column(
            "subscribe_id", sa.BigInteger(),
            sa.ForeignKey("subscribe.subscribe_id", ondelete="SET NULL"), nullable=True,
            comment="어느 구독이 끝났나",
        ),
        sa.Column(
            "reason", sa.Text(), nullable=False,
            comment="expensive|unused|missing|other_app|other",
        ),
        sa.Column(
            "offer_shown", sa.Boolean(), nullable=False,
            comment="해지방어 오퍼 시트가 실제로 떴는가(앱이 보낸 값 그대로)",
        ),
        sa.Column(
            "end_date_snapshot", sa.DateTime(timezone=True), nullable=True,
            comment="응답 시점 그 구독의 만료 시각(참고용, 판정에 안 씀)",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "reason IN ('expensive','unused','missing','other_app','other')",
            name="ck_churn_reason_reason",
        ),
    )
    op.create_index("ix_churn_reason_member_id", "churn_reason", ["member_id"])
    op.create_unique_constraint(
        "uq_churn_reason_member_subscribe", "churn_reason", ["member_id", "subscribe_id"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_table("churn_reason")
