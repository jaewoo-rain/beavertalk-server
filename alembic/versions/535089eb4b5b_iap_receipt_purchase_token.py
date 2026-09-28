# -*- coding: utf-8 -*-
"""iap_receipt.purchase_token / last_store_check_at — 구독 갱신 재조회 전제(§24)

앱 요청서(비버톡_서버전달_2026-09-28.md §1), docs/plans/2026-09-28-구독갱신-반영-24.md.
갱신을 받는 경로가 0건이고 만료는 검증 시점 한 번만 저장돼, 운영 store 구독 4/4건이
전부 만료됐다. 재조회(core/iap.py 의 verify() 재호출)로 고칠 수 있지만 그러려면
purchase_token 이 있어야 하는데 지금 iap_receipt·subscribe 어디에도 저장하지 않는다
— 이 마이그레이션이 그 전제를 놓는다.

⛔ 기존 5행 백필 불가 — 토큰이 어디에도 없다(그 회원들은 앱 「구매 복원」으로
채워진다, 전부 샌드박스 테스트 계정이라 실손 0). ⛔ 로그에 찍지 않는다(스토어
자격에 준한다) — core/iap.py·iap_service.py 어디도 이 값을 로그에 interpolate
하지 않는다(회귀로 고정).

Revision ID: 535089eb4b5b
Revises: a958b64ce7fd
"""

from alembic import op
import sqlalchemy as sa

revision = "535089eb4b5b"
down_revision = "a958b64ce7fd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "iap_receipt",
        sa.Column(
            "purchase_token", sa.Text(), nullable=True,
            comment="스토어 재조회용(§24). NULL=이 컬럼 추가 이전 receipt. 로그 금지",
        ),
    )
    op.add_column(
        "iap_receipt",
        sa.Column(
            "last_store_check_at", sa.DateTime(timezone=True), nullable=True,
            comment="마지막 스토어 재조회 시각(§24 쓰로틀용)",
        ),
    )
    op.create_index("ix_iap_receipt_purchase_token", "iap_receipt", ["purchase_token"])


def downgrade() -> None:
    op.drop_index("ix_iap_receipt_purchase_token", table_name="iap_receipt")
    op.drop_column("iap_receipt", "last_store_check_at")
    op.drop_column("iap_receipt", "purchase_token")
