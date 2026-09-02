"""B2B 교사 콘솔 — classroom / classroom_member / assignment / submission + member.is_teacher

과제 = 반 + TOPIK 챕터 1개 + 활동 1~3종 + 마감.
조직→반→좌석 3단이 아니라 **반 1단**이다(교사에 직결).
교사 전용 테이블을 만들지 않고 `member.is_teacher` 불리언 하나를 붙인다.

⛔ 초안은 `member.role` 에 'learner|teacher' 를 담으려 했다. **쓸 수 없다** —
   배포된 서버가 이미 같은 컬럼을 'user|admin'(운영 도구 접근 제어, `a4c8e1d7b209`)로
   쓰고 있다. 같은 컬럼을 다시 add 하면 마이그레이션이 실패하고, 값을 합치면
   「관리자이면서 교사」를 표현할 수 없다. 축을 나눈다.

★ 2026-09-02 리베이스 — `down_revision` 을 `c9d0e1f2a3b4` 에서 현행 배포 헤드
  `e2f3a4b5c6d7` 로 옮겼다. 초안이 만들어진 뒤 main 이 한참 앞서 나갔다.

설계: 23_제품기획_하네스/_output/2026-08-19_비버톡_B2B숙제/05_데이터모델.md

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d0e1f2a3b4c5"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── member.is_teacher ──
    # 기존 행은 전부 학습자다. NOT NULL + server_default 로 한 번에 채운다.
    # 인덱스는 걸지 않는다 — 교사는 전체의 극소수라 부분 인덱스가 아니면 이득이 없고,
    # 조회는 언제나 `member_id` 로 한 행을 집은 뒤 이 값을 읽는 형태다.
    op.add_column(
        "member",
        sa.Column(
            "is_teacher",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="B2B 교사 콘솔 접근 권한(기존 회원은 전부 false)",
        ),
    )

    # ── classroom ──
    op.create_table(
        "classroom",
        sa.Column("classroom_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("teacher_member_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, comment="반 이름(교사 자유입력 · 번역 금지)"),
        sa.Column("target_grade", sa.Integer(), nullable=False, comment="목표 TOPIK 급수(1~6)"),
        sa.Column("term", sa.Text(), nullable=True),
        sa.Column("join_code", sa.Text(), nullable=False, comment="참여코드 6자리(I·O·0·1 제외)"),
        sa.Column("capacity", sa.Integer(), nullable=False, server_default=sa.text("30")),
        sa.Column("code_expires_on", sa.Date(), nullable=True),
        sa.Column("institution", sa.Text(), nullable=True),
        sa.Column("teacher_display_name", sa.Text(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["teacher_member_id"], ["member.member_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("join_code", name="uq_classroom_join_code"),
        sa.CheckConstraint("target_grade BETWEEN 1 AND 6", name="ck_classroom_target_grade"),
        sa.CheckConstraint("capacity > 0", name="ck_classroom_capacity"),
    )
    op.create_index("ix_classroom_teacher_member_id", "classroom", ["teacher_member_id"])
    op.create_index("ix_classroom_archived_at", "classroom", ["archived_at"])
    op.create_index("ix_classroom_teacher_archived", "classroom", ["teacher_member_id", "archived_at"])

    # ── classroom_member ──
    op.create_table(
        "classroom_member",
        sa.Column("classroom_member_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("classroom_id", sa.BigInteger(), nullable=False),
        sa.Column("member_id", sa.BigInteger(), nullable=False),
        sa.Column("roster_name", sa.Text(), nullable=False, comment="반에서 쓸 이름(학습자 입력)"),
        sa.Column("student_no", sa.Text(), nullable=True),
        sa.Column("teacher_alias", sa.Text(), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["classroom_id"], ["classroom.classroom_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["member_id"], ["member.member_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("classroom_id", "member_id", name="uq_classroom_member"),
    )
    op.create_index("ix_classroom_member_classroom_id", "classroom_member", ["classroom_id"])
    op.create_index("ix_classroom_member_member_id", "classroom_member", ["member_id"])
    op.create_index("ix_classroom_member_left_at", "classroom_member", ["left_at"])
    op.create_index("ix_classroom_member_active", "classroom_member", ["classroom_id", "left_at"])

    # ── assignment ──
    op.create_table(
        "assignment",
        sa.Column("assignment_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("classroom_id", sa.BigInteger(), nullable=False),
        sa.Column("grade", sa.Integer(), nullable=False),
        sa.Column("chapter", sa.Integer(), nullable=False),
        sa.Column("activities", sa.Text(), nullable=False, comment="활동 JSON 배열"),
        sa.Column("target_item_ids", sa.Text(), nullable=False, comment="출제 시점 항목 스냅샷"),
        sa.Column("grammar_items", sa.Text(), nullable=True, comment="문법 표제 스냅샷(교재 예문 제외)"),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["classroom_id"], ["classroom.classroom_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("classroom_id", "grade", "chapter", "created_at", name="uq_assignment_slot"),
        sa.CheckConstraint("grade BETWEEN 1 AND 6", name="ck_assignment_grade"),
        sa.CheckConstraint("chapter > 0", name="ck_assignment_chapter"),
    )
    op.create_index("ix_assignment_classroom_id", "assignment", ["classroom_id"])
    op.create_index("ix_assignment_classroom_due", "assignment", ["classroom_id", "due_at"])

    # ── submission ──
    op.create_table(
        "submission",
        sa.Column("submission_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("assignment_id", sa.BigInteger(), nullable=False),
        sa.Column("classroom_member_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'not_started'")),
        sa.Column("speaking_passed", sa.Integer(), nullable=True),
        sa.Column("speaking_total", sa.Integer(), nullable=True),
        sa.Column("conversation_met", sa.Integer(), nullable=True),
        sa.Column("conversation_total", sa.Integer(), nullable=True),
        sa.Column("call_id", sa.BigInteger(), nullable=True),
        sa.Column("failed_item_ids", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignment.assignment_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["classroom_member_id"], ["classroom_member.classroom_member_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["call_id"], ["call.call_id"], ondelete="SET NULL"),
        sa.UniqueConstraint("assignment_id", "classroom_member_id", name="uq_submission_member"),
        sa.CheckConstraint(
            "status IN ('not_started', 'in_progress', 'done')", name="ck_submission_status"
        ),
    )
    op.create_index("ix_submission_assignment_id", "submission", ["assignment_id"])
    op.create_index("ix_submission_classroom_member_id", "submission", ["classroom_member_id"])
    op.create_index("ix_submission_call_id", "submission", ["call_id"])
    op.create_index("ix_submission_assignment_status", "submission", ["assignment_id", "status"])


def downgrade() -> None:
    op.drop_table("submission")
    op.drop_table("assignment")
    op.drop_table("classroom_member")
    op.drop_table("classroom")
    op.drop_column("member", "is_teacher")
