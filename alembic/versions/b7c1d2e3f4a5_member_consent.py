"""member_consent — 가입 동의 기록(항목·문서 버전·시각)

Revision ID: b7c1d2e3f4a5
Revises: a535dd899ae7
Create Date: 2026-10-17 10:00:00

PM-DEC-735·736 · 앱 빌드 72 동의 화면. 필수 4항목(age14·terms·privacy_collect·overseas_transfer)을
항목별 한 행으로 남긴다(추가 전용). 회원 하드 삭제 때 CASCADE.
downgrade 는 표를 지운다(동의 기록도 사라짐 — 되돌리기 전에 필요하면 표를 백업한다).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7c1d2e3f4a5"
down_revision: Union[str, None] = "a535dd899ae7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "member_consent",
        sa.Column("member_consent_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("member_id", sa.BigInteger(), sa.ForeignKey("member.member_id", ondelete="CASCADE"), nullable=False),
        sa.Column("item", sa.Text(), nullable=False, comment="동의 항목 코드(age14·terms·privacy_collect·overseas_transfer)"),
        sa.Column("doc_version", sa.Text(), nullable=False, comment="동의한 문서 버전(시행일 YYYY-MM-DD 등)"),
        sa.Column("agreed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
                  comment="동의 시각(서버 기준)"),
        sa.Column("app_version", sa.Text(), nullable=True, comment="동의한 앱 버전(예 1.0.0+72)"),
        sa.Column("platform", sa.Text(), nullable=True, comment="android·ios 등"),
        sa.Column("locale", sa.Text(), nullable=True, comment="동의 화면 UI 언어(BCP-47)"),
        comment="가입 동의 기록 — 항목별 한 행 · 추가 전용(PM-DEC-735·736)",
    )
    op.create_index("ix_member_consent_member_item", "member_consent", ["member_id", "item"])
    # 운영 public 표 71개가 모두 RLS 켜짐(2026-10-17 실측) — 정책 없이 켜서 Supabase 공개 API(anon 키)를 막는다.
    # 서버는 소유자 연결이라 영향 없음(399820b3abe2 와 같은 방식).
    op.execute("ALTER TABLE member_consent ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index("ix_member_consent_member_item", table_name="member_consent")
    op.drop_table("member_consent")
