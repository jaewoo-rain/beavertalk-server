"""member.role 추가 — /__dev 운영 도구 접근 제어(user|admin)

할인 이벤트 생성·레벨 초기화 같은 운영 도구가 "로그인한 아무 회원이나" 쓸 수 있는
상태였다. 관리자 개념을 도입한다.

Supabase JWT(app_metadata.role)가 아니라 우리 DB 컬럼으로 둔 이유:
  ① 권한 회수가 즉시 반영된다(JWT 는 토큰 만료 전까지 옛 권한이 살아 있다)
  ② CurrentMember 가 이미 member 행을 통째로 읽으므로 조회 비용이 0이다
  ③ 권한의 단일 소스가 우리 DB 가 된다(Supabase 와 이원화되지 않는다)

기존 행은 전부 'user'. 최초 관리자 승격은 데이터 작업으로 따로 한다(이 마이그레이션에
특정 이메일을 박으면 환경마다 계정이 달라 깨진다).

근거: docs/20260729_0453_한정할인-카운트다운과-할인이벤트-운영도구.md §4
"""

from alembic import op
import sqlalchemy as sa


revision = "a4c8e1d7b209"
down_revision = "e2f4a7c91b60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "member",
        sa.Column(
            "role",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'user'"),
            comment="권한(user|admin) — /__dev 운영 도구 접근 제어",
        ),
    )


def downgrade() -> None:
    op.drop_column("member", "role")
