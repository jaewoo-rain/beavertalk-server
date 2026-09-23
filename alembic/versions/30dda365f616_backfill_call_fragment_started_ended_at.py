"""옛 call 행 백필 — fragment_started_at/fragment_ended_at (QA C4 재검-4차)

04566cafd21b 가 두 컬럼을 추가했지만 기존 행은 둘 다 NULL 이다. 이 마이그레이션
없이는 "진행 중" 판정(fragment_started_at IS NOT NULL AND fragment_ended_at IS
NULL)이 옛 행을 **전부 "진행 중 아님"으로 폴백에 의존해서** 봐야 한다 — 코드에
`or call_date` 류 폴백을 남겨야 한다는 뜻이다.

⭐⭐ 결정(QA C4 재검-4차, 2026-09-23): 옛 행은 전부 **이미 끝난 것으로 백필**한다 —
    fragment_started_at = call_date(그 통화가 시작한 시각)
    fragment_ended_at   = updated_at(그 행에 마지막으로 쓰인 시각 — "끝났다"의 근사치)
지금 봤을 때 `status='ongoing'` 이라 진행 중처럼 보이는 옛 행도 **죽은 것으로
간주한다**(크래시·강제종료로 못 닫힌 세션 — 마이그레이션 시점에 실제로 살아있는
Live 세션은 없다, 배포 롤아웃 창은 별개로 관리한다). 이렇게 하면:
  - 코드에서 `call_date` 로의 폴백이 필요 없어진다 — normalcall 경로(정상 통화 생성)
    로 만들어진 행은 이제 옛 행이든 새 행이든 항상 두 값을 갖는다. (`CallService.
    create_call` 같은 별도 벌크 저장 경로는 여전히 NULL 로 남을 수 있다 — 그 경로는
    "이미 끝난 통화를 기록"하는 것이라 애초에 조각/진행 개념이 없다. NULL 은 그 뜻
    하나만 남는다: "이 통화는 실시간 조각 흐름을 탄 적이 없다".)
  - 옛 행의 예산 집계는 `total_time` 만 본다(진행 중이 아니므로 경과 추정이 안 붙는다).

Revision ID: 30dda365f616
Revises: 04566cafd21b
Create Date: 2026-09-23
"""

from alembic import op


revision = "30dda365f616"
down_revision = "04566cafd21b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE call SET fragment_started_at = call_date, fragment_ended_at = updated_at "
        "WHERE fragment_started_at IS NULL"
    )


def downgrade() -> None:
    # ⚠ 백필을 되돌리면 이 두 컬럼이 다시 전부 NULL 이 된다 — 옛 행이 다시 "판정
    #   불가"(코드가 폴백 없이는 못 다루는) 상태로 돌아간다는 뜻이다. 04566cafd21b
    #   자체를 내리는 게 아니라면 보통 이 downgrade 는 쓸 일이 없다.
    op.execute(
        "UPDATE call SET fragment_started_at = NULL, fragment_ended_at = NULL "
        "WHERE fragment_started_at = call_date AND fragment_ended_at = updated_at"
    )
