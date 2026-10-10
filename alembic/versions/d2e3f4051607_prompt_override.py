# -*- coding: utf-8 -*-
"""prompt_override — 배포 없이 대본을 고치는 테이블 (2026-10-10 사장님 지시)

## 왜
대본 한 줄을 고치려면 빌드+배포 5~8분이 든다(오늘 네 번 그렇게 기다렸다). 이 테이블에
글자를 넣으면 **다음 통화부터** 적용된다.

## ⛔ 기존 경로에 영향이 없다
- **새 테이블**이다. 읽거나 쓰는 코드가 늘 뿐, 기존 테이블·쿼리를 건드리지 않는다.
- ⚠ 이 DB 는 실서비스와 **공유**다(`app-api`·`demo-api`·`gpt-api` 가 같은 Supabase).
  그래서 코드 쪽 스위치 `PROMPT_OVERRIDE_ENABLED` 가 **기본 꺼짐**이다 — 이 브랜치가
  언젠가 머지돼도 스위치를 안 켜면 실서비스는 종전 그대로다.
- 행을 하나도 안 넣으면 아무 일도 안 일어난다(조회가 `None` 을 돌려주고 코드 기본값).

## append-only
행을 **덮어쓰지 않는다.** 고칠 때마다 새 행을 넣고 `enabled` 로 현역을 가린다 —
git 이력을 잃는 대신 이 테이블이 이력을 든다. 그리고 `call.prompt_override_id` 가
「그 통화가 어느 글자로 돌았나」를 든다(2026-10-10 에 1815·1817 을 Cloud Run 리비전으로
갈라야 했던 그 불편을 없앤다).

⚠ autogenerate 는 이 레포에서 안 돈다 — `DATABASE_URL_DIRECT` 가 없어 6543 pgbouncer 로
붙고 `json = unknown` 으로 깨진다. 손으로 썼다(`c1d2e3f40506` 과 같은 방식).
"""

import sqlalchemy as sa
from alembic import op

revision = "d2e3f4051607"
down_revision = "c1d2e3f40506"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prompt_override",
        sa.Column("override_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "name", sa.Text(), nullable=False,
            comment="대본 이름(expression/freetalk/chat/leveltest) — 코드 모듈명과 같아야 한다",
        ),
        sa.Column(
            "body", sa.Text(), nullable=False,
            comment="대본 전문. 슬롯 표기는 코드 기본값과 같아야 한다",
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("false"),
            comment="현역 여부. name 당 enabled=True 인 최신 행이 쓰인다",
        ),
        sa.Column("note", sa.Text(), nullable=True, comment="무엇을 왜 바꿨나(사람이 읽는 메모)"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        comment="프롬프트 override(append-only) — 배포 없이 대본을 고친다",
    )
    # 현역 조회가 `name` + `enabled` + 최신순이다 — 그 모양으로 인덱스를 둔다.
    op.create_index("ix_prompt_override_name_enabled", "prompt_override", ["name", "enabled"])

    # ⭐ 통화가 **어느 글자로 돌았나** — 이게 없으면 전사 분석에서 원인을 못 가른다.
    op.add_column(
        "call",
        sa.Column(
            "prompt_override_id", sa.BigInteger(), nullable=True,
            comment="이 통화가 쓴 prompt_override.override_id. NULL = 코드 기본값",
        ),
    )


def downgrade() -> None:
    op.drop_column("call", "prompt_override_id")
    op.drop_index("ix_prompt_override_name_enabled", table_name="prompt_override")
    op.drop_table("prompt_override")
