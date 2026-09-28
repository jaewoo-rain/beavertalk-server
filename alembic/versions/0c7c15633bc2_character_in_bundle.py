# -*- coding: utf-8 -*-
"""character.in_bundle — 캐릭터 묶음(bt_character_bundle) 구성의 정본(§2)

앱 요청서 §2. Play Developer API 실측(monetization.oneTimeProducts):
OneTimeProduct 스키마엔 자식 상품 필드가 없다 — 내용물은 영어 마케팅 설명
한 줄("Unlock Popo, Rara and Dudu, yours forever")로만 존재해 구조적으로 읽을
방법이 없다(애플도 동일). 그래서 이 컬럼이 묶음 구성의 유일한 정본이다.

Boolean NOT NULL DEFAULT false 로 추가 + popo·rara·dudu 3행만 true 로 세팅한다
(character_id 하드코딩 금지 — product_key 기준 UPDATE). 앞으로 추가되는 캐릭터는
기본 false 라 묶음에 자동으로 안 들어간다 — 넣고 싶으면 이 컬럼을 UPDATE 한
줄로 켠다(배포 불필요).

⚠ 이 컬럼을 바꾸면 Play Console 의 그 설명 문구도 사람이 같이 고쳐야 한다.

Revision ID: 0c7c15633bc2
Revises: 535089eb4b5b
"""

from alembic import op
import sqlalchemy as sa

revision = "0c7c15633bc2"
down_revision = "535089eb4b5b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "character",
        sa.Column(
            "in_bundle", sa.Boolean(), nullable=False, server_default=sa.text("false"),
            comment="캐릭터 묶음(bt_character_bundle)에 포함되는가 — 스토어엔 내용물 "
                    "필드가 없어 여기가 유일한 정본",
        ),
    )
    op.execute(
        "UPDATE character SET in_bundle = true "
        "WHERE lower(product_key) IN ('popo', 'rara', 'dudu')"
    )


def downgrade() -> None:
    op.drop_column("character", "in_bundle")
