# -*- coding: utf-8 -*-
"""call.expression_result — 표현학습 통화 1건의 **퀴즈 결과 스냅샷**(JSON)

⭐ 왜 컬럼이 필요한가(2026-09-10 QA 로 드러난 결함): 결과 화면을 `member_item_progress`
  에서 되짚으면 **나중 통화가 지난 결과를 지운다.**

    통화 A   「물」 드릴 → 오답        (drilled_call_id = A)
    통화 B   「물」 다시 드릴 → 통과   (drilled_call_id = B 로 **덮어쓴다**)
    ⇒ 통화 A 의 결과 화면을 다시 열면 「물」이 **사라진다.**

  그리고 선별이 «드릴했는데 못 끝낸 것» 을 다음 통화 맨 앞에 놓으므로(기획 §2-4)
  재드릴은 예외가 아니라 **정상 경로**다 ⇒ 틀린 항목만 조용히 증발하고 통과 항목만 남는다.

⭐ 그래서 «이 통화에서 무엇을 했나» 는 **통화의 사실**로 따로 적는다 — `call.summary`·
  `call.resume_context` 와 같은 성질이다(진행 상태가 아니라 그 통화의 기록).
  ⛔ 진도의 원본은 여전히 `member_item_progress` 다. 이 컬럼은 **파생 스냅샷**이고,
    화면 말고 아무도 읽지 않는다.

⚠ TEXT(JSON 문자열)다 — 프로젝트 컨벤션(테스트가 sqlite 인메모리라 JSONB 금지).

설계: docs/plans/2026-09-10-표현학습-프리토킹-통화-분리.md §5(통화 결과 화면)

Revision ID: 7d1f4b93ce2a
Revises: 9c4e17a2f8b3
"""

from alembic import op
import sqlalchemy as sa

revision = "7d1f4b93ce2a"
down_revision = "9c4e17a2f8b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "call",
        sa.Column(
            "expression_result", sa.Text(), nullable=True,
            comment="표현학습 퀴즈 결과 스냅샷(JSON 배열) — 결과 화면 전용 파생값",
        ),
    )


def downgrade() -> None:
    op.drop_column("call", "expression_result")
