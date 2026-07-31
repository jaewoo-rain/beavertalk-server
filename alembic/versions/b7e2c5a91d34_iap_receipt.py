"""iap_receipt 추가 — 스토어 영수증 원장(멱등성)

같은 영수증이 여러 번 오는 건 정상 동작이다(네트워크 재시도·앱 재실행·구매 복원).
UNIQUE(platform, transaction_id) 로 중복 지급을 DB 레벨에서 막는다 — 애플리케이션
검사만으로는 동시 요청 경합에서 샌다.

계약: docs/20260731_1230_IAP-API-계약서-프론트공유용.md
"""

from alembic import op
import sqlalchemy as sa


revision = "b7e2c5a91d34"
down_revision = "a4c8e1d7b209"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "iap_receipt",
        sa.Column("iap_receipt_id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("member_id", sa.BigInteger(), nullable=False, comment="지급받은 회원"),
        sa.Column("platform", sa.Text(), nullable=False, comment="ios | android"),
        sa.Column("transaction_id", sa.Text(), nullable=False,
                  comment="iOS originalTransactionId / Android orderId"),
        sa.Column("product_id", sa.Text(), nullable=False, comment="스토어 상품 ID"),
        sa.Column("kind", sa.Text(), nullable=False, comment="character | subscription"),
        sa.Column("character_id", sa.BigInteger(), nullable=True,
                  comment="캐릭터 지급이면 그 id(구독이면 NULL)"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True,
                  comment="구독 만료(캐릭터는 NULL)"),
        sa.Column("is_sandbox", sa.Boolean(), nullable=False, server_default=sa.text("false"),
                  comment="테스트 결제 여부(운영 집계에서 제외)"),
        sa.Column("is_stub", sa.Boolean(), nullable=False, server_default=sa.text("false"),
                  comment="스텁 검증(실검증 아님) — 운영 집계 제외"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["member.member_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("iap_receipt_id"),
        sa.UniqueConstraint("platform", "transaction_id", name="uq_iap_platform_tx"),
    )
    op.create_index("ix_iap_receipt_member_id", "iap_receipt", ["member_id"])


def downgrade() -> None:
    op.drop_index("ix_iap_receipt_member_id", table_name="iap_receipt")
    op.drop_table("iap_receipt")
