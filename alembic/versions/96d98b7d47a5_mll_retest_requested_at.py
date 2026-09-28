# -*- coding: utf-8 -*-
"""member_language_level.retest_requested_at — 레벨 초기화를 통화 성립 뒤로(§9)

앱 요청서 §3(2026-09-27, docs/plans/2026-09-27-앱요청-20건-잔여.md). 옛 동작은
"다시하기" 요청 즉시 member_language_level 행을 지웠다(ko 면 korean_level 도
NULL) — 그런데 레벨테스트 거절(ALREADY_IN_CALL·DAILY_LIMIT)은 실제 통화 시작
시점에서만 걸린다. 그 사이(연결 중 취소·다른 기기 통화 중)에 실패하면 레벨만
지워진 채 아무것도 대신 채워지지 않는다(실측: member 58명 중 레벨 없음 37명,
그중 통화 기록은 있는데 레벨테스트 통화가 0건인 회원 다수 — member 10·91·8·17·
18·9 등).

이 마이그레이션은 `retest_requested_at`(NULL 허용) 컬럼 하나만 추가한다:
- 「다시하기」요청은 이제 이 시각만 찍는다(행 삭제 없음).
- 실제 초기화(행 삭제·korean_level NULL)는 레벨테스트 통화가 call_started 로
  성립한 뒤(mastery_service.apply_pending_retest)로 옮긴다.

⛔ 백필 불필요 — 새 칸은 기존 행 전부 NULL 이고, NULL = "재측정 대기 없음"이
모든 기존 행에 대해 이미 올바른 뜻이다(과거 데이터를 복구/유추할 정보가 없다 —
bt-back 지시: 신규가입으로 레벨이 없던 것과 이 버그로 지워진 것을 구분할 수
없으므로 과거는 그대로 둔다, 이번 작업은 앞으로만 막는다).

Revision ID: 96d98b7d47a5
Revises: d3e5f7a9b1c2
"""

from alembic import op
import sqlalchemy as sa

revision = "96d98b7d47a5"
down_revision = "d3e5f7a9b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "member_language_level",
        sa.Column(
            "retest_requested_at", sa.DateTime(timezone=True), nullable=True,
            comment=(
                "§9 「다시하기」 요청 시각(NULL=대기 없음). 실제 초기화는 레벨테스트 "
                "통화 call_started 성립 시점(apply_pending_retest)으로 미룬다."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("member_language_level", "retest_requested_at")
