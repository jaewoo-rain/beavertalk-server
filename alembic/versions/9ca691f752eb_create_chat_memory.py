"""자유대화 기억 저장소 — chat_memory 테이블 생성 (C6)

docs/plans/2026-09-22-프리미엄-자유대화-15분-달력.md 의 C6. 회원·학습 대상 언어별로
자유대화 누적 요약(summary)·화제(topics)·사실(facts)·관심사(interests)·다음 화제
(next_topics)를 한 행에 담는다.

⚠ 문서 원문은 "PK (member_id, language)"이지만, 이 코드베이스의 모든 회원×축 테이블은
  대리 PK + UniqueConstraint 패턴이라(합성 PK 모델이 하나도 없다) 그 관례를 따른다
  (chat_memory.py 모델 주석 참조) — 뜻은 UniqueConstraint 로 그대로 강제된다.

⛔ JSONB 아님 — 프로젝트 규약(sqlite 테스트 호환, 다른 도메인 JSON 컬럼과 동일).

Revision ID: 9ca691f752eb
Revises: 30dda365f616
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "9ca691f752eb"
down_revision = "30dda365f616"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_memory",
        sa.Column("chat_memory_id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("member_id", sa.BigInteger(), nullable=False, comment="회원"),
        sa.Column("language", sa.Text(), nullable=False, comment="학습 대상 언어(ISO 639-1)"),
        sa.Column(
            "summary", sa.Text(), nullable=False, server_default=sa.text("''"),
            comment="누적 압축 요약(자유 텍스트, 상한 1200자) — merge 때마다 옛 요약+이번 통화 요약을 재압축",
        ),
        sa.Column(
            "topics", sa.JSON(), nullable=False, server_default=sa.text("'[]'"),
            comment="최근 화제(최신 우선, 최대 10)",
        ),
        sa.Column(
            "facts", sa.JSON(), nullable=False, server_default=sa.text("'[]'"),
            comment="학습자에 대해 알게 된 사실(최신 우선, 최대 15)",
        ),
        sa.Column(
            "interests", sa.JSON(), nullable=False, server_default=sa.text("'[]'"),
            comment="대화에서 드러난 관심사(최신 우선, 최대 10)",
        ),
        sa.Column(
            "next_topics", sa.JSON(), nullable=False, server_default=sa.text("'[]'"),
            comment="다음 통화에서 이어 말할 거리(최신 우선, 최대 5)",
        ),
        sa.Column(
            "last_call_id", sa.BigInteger(), nullable=True,
            comment="이 값으로 이미 merge 했으면 다시 안 한다(멱등)",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("chat_memory_id"),
        sa.UniqueConstraint("member_id", "language", name="uq_chat_memory_member_language"),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.member_id"], ondelete="CASCADE", name="fk_chat_memory_member",
        ),
        sa.ForeignKeyConstraint(
            ["last_call_id"], ["call.call_id"], ondelete="SET NULL", name="fk_chat_memory_last_call",
        ),
    )


def downgrade() -> None:
    op.drop_table("chat_memory")
