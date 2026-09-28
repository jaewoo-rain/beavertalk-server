# -*- coding: utf-8 -*-
"""call.was_spoken — 「학습자가 참여했다」를 통화 종료 시점에 확정(§12, N=60)

앱 요청서 §12(2026-09-27, bt-back 실측 승인 N=60). `call_repository._spoke_exists()`
가 지금까지 읽을 때마다 `call_raw_data` 에 내용 있는 user 행이 있는지 다시 계산했다
(파생·불안정) — 전사 파이프라인이 실패하면(운영 실측 49건, 최장 324초) 통화가
아무리 길어도 "말 안 함"으로 잡혀 연속일·하루 한도·이어하기 판정이 조용히 샜다.

이 마이그레이션이 하는 일 둘:
1. `call.was_spoken`(NOT NULL, 기본 false) 컬럼 추가.
2. 기존 1,615행을 **같은 커밋 안에서** 백필한다 — 코드 배포와 분리하면 그 사이
   전체가 false 로 보여 모든 사용자의 연속일이 0 이 되는 장애 구간이 생긴다(bt-back
   지시).

기준(설계 (다), bt-back 승인) — 전사 있음(옛 기준, role='user' AND content 비어있지
않음) **OR** total_time >= 60초(`SPOKEN_MIN_TOTAL_TIME_S`, call_repository.py).
후보 60/90/120/300초 중 실측(spoke=false 564건의 total_time 분포 + 후보별 "새로
뒤집히는 통화·새로 말한 날" 영향)으로 60을 골랐다 — <30초 470건(83%, 평균 17초,
무음 3단 넛지→종료 패턴)은 그대로 제외되고, 60초 넘는데 전사가 빈 49건(우리 쪽
전사 실패로 판단)만 구제된다. 이 SQL 은 그 실측 당시의 기준을 그대로 반영한다 —
모델의 `SPOKEN_MIN_TOTAL_TIME_S` 가 나중에 바뀌어도 이 마이그레이션(과거 한 시점의
백필)은 그 시점 기준(60)으로 고정해 둔다(마이그레이션은 재실행되지 않는다).

⚠ sqlite(테스트)는 이 마이그레이션을 타지 않는다 — `create_all` 이 모델의
server_default(false)를 쓴다. 테스트는 `finalize_call`/각 테스트 헬퍼가 직접
`was_spoken` 을 채운다.

Revision ID: d3e5f7a9b1c2
Revises: c2d4e6f8a0b1
"""

from alembic import op
import sqlalchemy as sa

revision = "d3e5f7a9b1c2"
down_revision = "c2d4e6f8a0b1"
branch_labels = None
depends_on = None

_SPOKEN_MIN_TOTAL_TIME_S = 60  # 백필 당시 기준 — call_repository.SPOKEN_MIN_TOTAL_TIME_S 와 같은 값이어야 한다(지금은 같다)


def upgrade() -> None:
    op.add_column(
        "call",
        sa.Column(
            "was_spoken", sa.Boolean(), nullable=False, server_default=sa.text("false"),
            comment="학습자가 이 통화에서 참여했나(전사 있음 OR total_time>=60) — finalize_call 확정",
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE call
               SET was_spoken = true
             WHERE total_time >= :min_s
                OR EXISTS (
                     SELECT 1 FROM call_raw_data r
                      WHERE r.call_id = call.call_id
                        AND r.role = 'user'
                        AND r.content IS NOT NULL
                        AND r.content <> ''
                   )
            """
        ).bindparams(min_s=_SPOKEN_MIN_TOTAL_TIME_S)
    )


def downgrade() -> None:
    op.drop_column("call", "was_spoken")
