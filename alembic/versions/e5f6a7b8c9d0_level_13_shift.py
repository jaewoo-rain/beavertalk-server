"""level 13-shift — 12레벨 → 13레벨 재편 (레벨 1 = 생존 회화)

기존 level.level_no 1~12(교재 12권)를 2~13 으로 밀고, 1번 자리를 신규
'생존 회화' 레벨용으로 비운다. member.korean_level 도 +1 동반 이동.

- 2단 shift(+100 → −99)인 이유: uq_level_no 가 비지연(non-deferrable) UNIQUE 라
  단순 +1 은 행 갱신 순서에 따라 중간 충돌 — 전 행을 충돌 불가 대역(101~112)으로
  옮긴 뒤 최종값(2~13)으로 내린다.
- 사전상태 가드: level 이 빈 테이블이면 shift 전체 스킵(신규 DB — 시드가 1~13 직접
  적재), 12행(1~12)이면 진행, 그 외(이미 shift 된 2~13 포함)는 RuntimeError.
  member.korean_level 도 max ≤ 12 아니면 에러 — 이중 shift(재실행) 사고 방지.
- 생존 레벨(level_no=1) 행 삽입은 이 마이그레이션이 하지 않는다 — seed_levels 담당.
- ⚠ 파괴적·비가역(R6): 실행 전 반드시 prod 백업 + 사장님 확인.
  downgrade 는 차단(레벨테스트·체크판이 1~13 좌표로 기록을 시작하면 역-shift 로
  복원 불가) — 되돌리려면 백업 복원으로만.
- 배포 순서: 레벨테스트 기능(P1)보다 먼저 — 판정이 처음부터 1~13 좌표를 쓰도록.

설계: docs/20260709_1231_level-system-master-plan.md §3.4 Rev1.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-09 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()

    # ── 사전상태 가드: 기대 상태(12행 1~12)에서만 shift, 빈 테이블은 no-op ──
    cnt, lo, hi = bind.execute(sa.text(
        "SELECT count(*), COALESCE(min(level_no), 0), COALESCE(max(level_no), 0) FROM level"
    )).one()
    if (cnt, lo, hi) == (0, 0, 0):
        # 신규(빈) DB — shift 대상 없음. level 1~13 은 seed_levels 가 직접 적재.
        return
    if (cnt, lo, hi) != (12, 1, 12):
        raise RuntimeError(
            f"level 사전상태 불일치 — count={cnt}, min={lo}, max={hi} "
            "(기대: 빈 테이블 또는 12행 1~12. 이미 shift 된 DB 재실행 금지 — 백업 확인)"
        )
    max_kl = bind.execute(sa.text(
        "SELECT COALESCE(max(korean_level), 0) FROM member"
    )).scalar_one()
    if max_kl > 12:
        raise RuntimeError(
            f"member.korean_level 사전상태 불일치 — max={max_kl} (기대 ≤ 12. "
            "이미 13레벨 좌표로 shift 된 것으로 보임 — 재실행 금지)"
        )

    # 1~12 → 2~13 (uq_level_no 비지연 충돌 회피 2단 shift)
    op.execute("UPDATE level SET level_no = level_no + 100")
    op.execute("UPDATE level SET level_no = level_no - 99")
    # 회원 레벨 동반 이동 (NULL = 레벨테스트 미실시 — 그대로 둔다)
    op.execute(
        "UPDATE member SET korean_level = korean_level + 1 "
        "WHERE korean_level IS NOT NULL"
    )


def downgrade() -> None:
    """Downgrade schema."""
    raise RuntimeError("비가역: 13레벨 재편 후 다운그레이드 불가 — 백업 복원으로만")
