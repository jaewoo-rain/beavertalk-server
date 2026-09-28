# -*- coding: utf-8 -*-
"""call.summary_lang + cur_text_i18n — §6 차시 제목 · §10 통화 제목 다국어(2026-09-29)

- call.summary_lang (Text NULL): 요약을 실제로 쓴 언어. 과거 행은 NULL(백필 없음 — 목록은
  한글 유무로만 ko/비-ko 를 가른다).
- cur_text_i18n: 커리큘럼 표시 문구 번역 캐시(요청 시 번역). (kind, source, locale) 유니크,
  ko 행 금지(CHECK). 빈 표로 시작 — 미리 채우지 않는다.

⛔ §10 의 번역 캐시 `call_summary_translation` 은 **여기서 만들지 않는다** — B2B alembic
   `e1f2a3b4c5d6` 이 이미 운영에 만들었다(domains/learning/models/call_summary_translation.py).

문서: docs/20260929_0050_차시제목-통화제목-다국어-요청시번역.md

Revision ID: 2d65f20ea0c8
Revises: 4cec70d04fc0
"""

import sqlalchemy as sa
from alembic import op

revision = "2d65f20ea0c8"
down_revision = "4cec70d04fc0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "call",
        sa.Column(
            "summary_lang", sa.Text(), nullable=True,
            comment="summary 를 생성한 언어(ISO 639-1). NULL=미기록(과거 행)",
        ),
    )
    op.create_table(
        "cur_text_i18n",
        sa.Column("cur_text_i18n_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False,
                  comment="무엇의 번역인가 — cur_situation(cur_lesson.situation)"),
        sa.Column("source", sa.Text(), nullable=False,
                  comment="한국어 원문 그대로(키) — 원문이 바뀌면 새 행"),
        sa.Column("locale", sa.Text(), nullable=False,
                  comment="번역 언어(ISO 639-1, member.language 축) — ko 금지"),
        sa.Column("text", sa.Text(), nullable=False, comment="번역문"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False, comment="생성 시각"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False, comment="수정 시각"),
        sa.UniqueConstraint("kind", "source", "locale", name="uq_cur_text_i18n"),
        sa.CheckConstraint("locale <> 'ko'", name="ck_cur_text_i18n_not_ko"),
    )


def downgrade() -> None:
    op.drop_table("cur_text_i18n")
    op.drop_column("call", "summary_lang")
