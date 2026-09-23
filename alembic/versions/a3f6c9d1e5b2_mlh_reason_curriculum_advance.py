# -*- coding: utf-8 -*-
"""member_level_history.reason 에 'curriculum_advance' 추가

docs/plans/2026-09-23-레벨-커리큘럼-연결.md L3. 진도 포인터(cur_member_progress)가
레벨 경계를 넘으면 레벨을 올린다 — 이건 `expression_complete`(옛 경로 전용, 그 레벨
퀴즈 전량 통과)와 **판정 기준이 다르다**. cur 경로(운영 기본, CUR_ENABLED=True)는
옛 승급 함수(promote_by_expression)를 아예 안 부른다(call_session.py:4123-4124 —
시험이 0회를 잠근다) — 그래서 두 승급 사슬이 같은 값을 쓰면 «이 레벨은 어느 기준으로
올라간 것인가» 를 감사로그에서 되물을 수 없다(c5a8e206d41f 가 gate_promotion 과
expression_complete 를 가른 것과 같은 이유).

⚠ CHECK 제약은 값 목록이라 **드롭 후 재생성**한다(Postgres). 기존 행은 옛 6개 값 중
  하나라 재생성에 걸리지 않는다(member_level_history 225행, 검증 스캔 부담 0).
⚠ sqlite(테스트)는 이 마이그레이션을 타지 않는다 — `create_all` 이 모델의 제약을 쓴다.
  그래서 모델과 이 파일이 **같은 목록**이어야 한다(어긋나면 운영에서만 터진다).

Revision ID: a3f6c9d1e5b2
Revises: f2a8c61e9d34
"""

from alembic import op

revision = "a3f6c9d1e5b2"
down_revision = "f2a8c61e9d34"
branch_labels = None
depends_on = None

_OLD = (
    "reason IN ('placement', 'gate_promotion', 'remeasure_up', 'remeasure_down',"
    " 'manual', 'expression_complete')"
)
_NEW = (
    "reason IN ('placement', 'gate_promotion', 'remeasure_up', 'remeasure_down',"
    " 'manual', 'expression_complete', 'curriculum_advance')"
)


def upgrade() -> None:
    op.drop_constraint("ck_mlh_reason", "member_level_history", type_="check")
    op.create_check_constraint("ck_mlh_reason", "member_level_history", _NEW)


def downgrade() -> None:
    # ⚠ 되돌리기 전에 'curriculum_advance' 행이 있으면 이 제약이 **거부한다**(의도한
    #   대로다 — 조용히 지우는 것보다 낫다). 필요하면 그 행들을 먼저 처리해라.
    op.drop_constraint("ck_mlh_reason", "member_level_history", type_="check")
    op.create_check_constraint("ck_mlh_reason", "member_level_history", _OLD)
