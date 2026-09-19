"""call_raw_data — (call_id, turn_index) 유니크 제약: 전사 중복 저장 재발 방지

배경: 1644(2026-09-18) 에서 call_raw_data 가 75행인데 고유 turn 65개였다 — 점진 flush 가 «저장 뒤에» 커서를 올려서,
fragment_end 로 await 가 취소되면(스레드의 commit 은 끝난다) 종료 저장이 같은 구간을 다시 썼다. 12차 A 에서 커서 순서와
코드 멱등을 고쳤고, 13차 A 에서 옛 중복 400행(통화 39건)을 지웠다 ⇒ 이제 DB 가 막는다.
  · uq_call_raw_data_call_turn UNIQUE (call_id, turn_index)
  · turn_index 는 nullable — Postgres 는 NULL 을 서로 다른 값으로 보므로 번호 없는 옛 행은 걸리지 않는다.
  · 저장 경로는 이미 사전 조회로 건너뛴다(normalcall_service.save_segments). 경합으로 제약에 걸리면 그 행만 SAVEPOINT 로
    되돌리고 건너뛴다 — 통화 저장 전체를 죽이지 않는다(R5).
⚠ 적용 전 중복이 남아 있으면 생성이 거부된다 — scripts/dev_dedupe_raw_data.py --all 로 먼저 확인한다.
⛔ 워크트리에서 upgrade 를 돌리지 않는다. 운영 DB 적용은 사장님 «적용» 뒤 bt-back 이 한다.

Revision ID: a9e4d7c31f80
Revises: c3d4e5f6a7b8
Create Date: 2026-09-19
"""
from alembic import op

revision = "a9e4d7c31f80"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_call_raw_data_call_turn", "call_raw_data", ["call_id", "turn_index"])


def downgrade() -> None:
    op.drop_constraint("uq_call_raw_data_call_turn", "call_raw_data", type_="unique")
