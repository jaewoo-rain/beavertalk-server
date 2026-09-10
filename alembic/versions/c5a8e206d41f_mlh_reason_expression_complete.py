# -*- coding: utf-8 -*-
"""member_level_history.reason 에 'expression_complete' 추가

⭐ 표현학습 승급(D12 — «그 레벨 전체 퀴즈 통과»)은 옛 게이트(`gate_promotion`, G1∧G2)와
  **판정 기준이 다르다.** 그래서 이력의 이유를 갈라 적는다.

⛔ `gate_promotion` 으로 뭉뚱그리지 마라. 두 사슬이 공존하는 동안 «이 레벨은 어느 기준으로
  올라간 것인가» 를 되물을 수 없게 되고, 그건 감사 불가능이라는 뜻이다 — 승급은 학습자에게
  보이는 결과라 나중에 반드시 되묻게 된다.

⚠ CHECK 제약은 값 목록이라 **드롭 후 재생성**한다(Postgres). 기존 행은 전부 옛 5개 값 중
  하나라 재생성에 걸리지 않는다.
⚠ sqlite(테스트)는 이 마이그레이션을 타지 않는다 — `create_all` 이 모델의 제약을 쓴다.
  그래서 모델과 이 파일이 **같은 목록**이어야 한다(어긋나면 운영에서만 터진다).

Revision ID: c5a8e206d41f
Revises: 7d1f4b93ce2a
"""

from alembic import op

revision = "c5a8e206d41f"
down_revision = "7d1f4b93ce2a"
branch_labels = None
depends_on = None

_OLD = (
    "reason IN ('placement', 'gate_promotion', 'remeasure_up', 'remeasure_down', 'manual')"
)
_NEW = (
    "reason IN ('placement', 'gate_promotion', 'remeasure_up', 'remeasure_down',"
    " 'manual', 'expression_complete')"
)


def upgrade() -> None:
    op.drop_constraint("ck_mlh_reason", "member_level_history", type_="check")
    op.create_check_constraint("ck_mlh_reason", "member_level_history", _NEW)


def downgrade() -> None:
    # ⚠ 되돌리기 전에 'expression_complete' 행이 있으면 이 제약이 **거부한다**(의도한 대로다 —
    #   조용히 지우는 것보다 낫다). 필요하면 그 행들을 먼저 처리해라.
    op.drop_constraint("ck_mlh_reason", "member_level_history", type_="check")
    op.create_check_constraint("ck_mlh_reason", "member_level_history", _OLD)
