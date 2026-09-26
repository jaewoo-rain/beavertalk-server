# -*- coding: utf-8 -*-
"""payment·subscribe·iap_receipt.member_id 를 nullable + ON DELETE SET NULL 로

S2(2026-09-26, Play 심사 대비) — 계정 삭제를 소프트 삭제에서 하드 삭제(DELETE FROM
member, 나머지 FK 는 CASCADE)로 바꾼다. 결제 3표(payment·subscribe·iap_receipt)는
법무 보존 의무 때문에 함께 지워지면 안 된다 — 그런데 이 세 컬럼이 지금 NOT NULL 이라
"연결만 끊기"(SET NULL)가 안 되고 CASCADE 로 같이 지워진다.

⛔ NOT NULL 을 풀기만 하고 FK 의 ondelete 를 안 바꾸면 여전히 CASCADE 로 지워진다
  (동작이 안 바뀐다) — 두 가지를 반드시 같이 한다.
⚠ 신원 소실은 지금은 그대로 둔다(스냅샷 컬럼 없음, YAGNI) — 「5년」 보존 기간·항목은
  법무 확정 대기(코드에 근거 없음, 요청서 값). 필요해지면 컬럼을 나중에 붙인다.
⚠ 이 세 FK 는 이름을 준 적이 없어 Postgres 기본 명명(`<table>_<column>_fkey`)을 쓴다
  — drop_constraint 로 그 이름을 지정해 지우고 같은 이름으로 다시 만든다.
⚠ sqlite(테스트)는 이 마이그레이션을 타지 않는다 — `create_all` 이 모델의 제약을
  쓴다. 그래서 모델(`domains/commerce/models/{payment,subscribe,iap_receipt}.py`)과
  이 파일이 **같은 내용**이어야 한다(어긋나면 운영에서만 터진다).

Revision ID: c2d4e6f8a0b1
Revises: a3f6c9d1e5b2
"""

from alembic import op

revision = "c2d4e6f8a0b1"
down_revision = "a3f6c9d1e5b2"
branch_labels = None
depends_on = None

_TABLES = ("payment", "subscribe", "iap_receipt")


def upgrade() -> None:
    for table in _TABLES:
        fk_name = f"{table}_member_id_fkey"
        op.drop_constraint(fk_name, table, type_="foreignkey")
        op.alter_column(table, "member_id", nullable=True)
        op.create_foreign_key(
            fk_name, table, "member", ["member_id"], ["member_id"], ondelete="SET NULL",
        )


def downgrade() -> None:
    for table in _TABLES:
        fk_name = f"{table}_member_id_fkey"
        op.drop_constraint(fk_name, table, type_="foreignkey")
        # ⚠ 되돌리기 전에 member_id IS NULL 행이 있으면 NOT NULL 복원이 실패한다
        #   (탈퇴 회원의 결제 기록이 이미 생겼을 수 있다) — 그 행은 운영에서 직접
        #   정리하거나 이 다운그레이드를 쓰지 않는다.
        op.alter_column(table, "member_id", nullable=False)
        op.create_foreign_key(
            fk_name, table, "member", ["member_id"], ["member_id"], ondelete="CASCADE",
        )
