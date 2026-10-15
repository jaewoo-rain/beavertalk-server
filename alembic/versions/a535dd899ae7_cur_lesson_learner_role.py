"""cur_lesson.learner_role — 차시 학습자 역할(학습자가 맡는 역할 한 줄)

Revision ID: a535dd899ae7
Revises: 399820b3abe2
Create Date: 2026-10-16 10:00:00

PM-DEC-663 · SV-33(52 하네스 통화흐름 공동설계 04_변경목록). `partner`(상대역)와 짝을 이루는
«학습자 쪽 역할» 칸이다. NULL 허용·기본값 없음 — 값은 별도 적재(52 `05_학습자역할_결과.jsonl`
1,390행)와 시드 `cur_seed*.json` 의 `lessons[].learner_role` 로 들어온다.
표시 번역은 `cur_text_i18n` kind `cur_learner_role`(원문 키 · cur_situation·cur_partner 와 같은 방식).
downgrade 는 칸만 지운다(번역 행은 원문 키 캐시라 남겨도 무해).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a535dd899ae7"
down_revision: Union[str, None] = "399820b3abe2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cur_lesson",
        sa.Column(
            "learner_role", sa.Text(), nullable=True,
            comment="학습자 역할 — 학습자가 맡는 쪽(partner 의 짝). NULL=미정",
        ),
    )


def downgrade() -> None:
    op.drop_column("cur_lesson", "learner_role")
