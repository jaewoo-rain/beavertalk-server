"""구독 3티어 재편 — subscribe 상태/플랜 컬럼 + character.product_key

왜: 디자인이 구독 상태 8종(free/trial/active_pro/active_max/grace/on_hold/
ending/expired)을 전제하는데, subscribe 는 5필드뿐이라 4종밖에 판정하지 못했다.
나머지는 서버에 데이터가 아예 없어 앱이 원천적으로 판정 불가였다.

두 축을 나눈다: **상태**(billing_state·is_trial·is_activate)와 **플랜**(plan).
grace/on_hold/ending 은 직전 플랜을 유지하므로 상태만으로 플랜을 알 수 없다.
앱도 같은 구조다(subscription_state.dart — impliedTier 가 이 셋에서 null).

character.product_key: 스토어 상품 ID(bt_character_{key})용 **불변 슬러그**.
name 은 바뀔 수 있고(스토어 상품 ID 는 영구 불변), character_id 는 dev/prod 가
다르다(prod 2·9·10·11 / dev 2·3·4·5). 둘 다 영구 식별자로 못 쓴다.

백필 규칙:
  - 기존 subscribe 행은 전부 pro·비체험·정상결제로 소급한다(현행 판매 상품이
    Pro 월납 1종뿐이라 사실과 일치).
  - source 는 price 로 가른다 — IAP 가 만든 행은 금액을 비워 둔다("스토어가
    청구한다 — 서버는 금액을 모른다", iap_service._grant_subscription). 레거시
    POST /subscriptions 는 항상 금액이 있다.
  - product_key 는 lower(영숫자만 남긴 name).

⚠ 회원당 활성 행 부분 UNIQUE 인덱스는 **여기서 만들지 않는다**. 레거시
POST /subscriptions 가 중복 검사 없이 행을 만들어 와서, 기존 데이터에 중복이
있으면 인덱스 생성이 실패한다. 데이터 정리는 파괴적이라 별도 확인 후 진행하고,
이번엔 애플리케이션 레벨 409 가드만 건다.

계획: docs/20260804_2353_구독-3티어-재편-구현계획.md

Revision ID: a1b2c3d4e5f6
Revises: 3549a9133f9d
Create Date: 2026-08-04 23:58:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "3549a9133f9d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UQ_PRODUCT_KEY = "uq_character_product_key"
_IX_PRODUCT_KEY = "ix_character_product_key"


def upgrade() -> None:
    """Upgrade schema."""
    # ── subscribe ────────────────────────────────────────────────────────── #
    op.add_column(
        "subscribe",
        sa.Column(
            "plan", sa.String(8), nullable=False, server_default="pro",
            comment="pro | max",
        ),
    )
    op.add_column(
        "subscribe",
        sa.Column(
            "billing_period", sa.String(8), nullable=True,
            comment="monthly | yearly (스토어 상품에서 파생)",
        ),
    )
    op.add_column(
        "subscribe",
        sa.Column("product_id", sa.Text(), nullable=True, comment="스토어 상품 ID 원본"),
    )
    op.add_column(
        "subscribe",
        sa.Column(
            "source", sa.String(8), nullable=False, server_default="manual",
            comment="manual | store",
        ),
    )
    op.add_column(
        "subscribe",
        sa.Column(
            "is_trial", sa.Boolean(), nullable=False, server_default=sa.false(),
            comment="체험 기간인가(앱은 체험을 Max 로 취급)",
        ),
    )
    op.add_column(
        "subscribe",
        sa.Column(
            "billing_state", sa.String(16), nullable=False, server_default="ok",
            comment="ok | grace | on_hold",
        ),
    )
    op.add_column(
        "subscribe",
        sa.Column(
            "retrying_until", sa.DateTime(timezone=True), nullable=True,
            comment="grace 에서만 값 존재",
        ),
    )
    op.add_column(
        "subscribe",
        sa.Column(
            "paused_since", sa.DateTime(timezone=True), nullable=True,
            comment="on_hold 에서만 값 존재",
        ),
    )
    # IAP 가 만든 행(금액 없음)만 store 로 표시. 나머지는 server_default 대로 manual.
    op.execute("UPDATE subscribe SET source = 'store' WHERE price IS NULL")

    # ── character.product_key ────────────────────────────────────────────── #
    # 1) nullable 로 추가 → 2) 백필 → 3) NOT NULL 승격. NOT NULL 로 바로 추가하면
    #    기존 행이 있는 테이블에서 실패한다.
    op.add_column(
        "character",
        sa.Column(
            "product_key", sa.String(32), nullable=True,
            comment="스토어 상품 ID 슬러그(불변)",
        ),
    )
    # 영숫자만 남기고 소문자화. 이름이 비었거나 특수문자뿐이면 PK 기반 폴백을 써서
    # NOT NULL 승격이 막히지 않게 한다(그런 행이 실제로 있을 이유는 없지만, 백필이
    # 중간에 죽으면 원인 추적이 어렵다).
    op.execute(
        """
        UPDATE character
           SET product_key = COALESCE(
                   NULLIF(lower(regexp_replace(name, '[^A-Za-z0-9]', '', 'g')), ''),
                   'c' || character_id::text
               )
        """
    )
    # 동명이인(대소문자만 다른 이름 등)이 있으면 UNIQUE 가 실패한다 — 그 경우에만
    # id 접미사를 붙여 유일하게 만든다.
    op.execute(
        """
        UPDATE character c
           SET product_key = c.product_key || '_' || c.character_id::text
          FROM (
                SELECT product_key
                  FROM character
                 GROUP BY product_key
                HAVING count(*) > 1
               ) dup
         WHERE c.product_key = dup.product_key
        """
    )
    op.alter_column("character", "product_key", nullable=False)
    op.create_index(_IX_PRODUCT_KEY, "character", ["product_key"])
    op.create_unique_constraint(_UQ_PRODUCT_KEY, "character", ["product_key"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(_UQ_PRODUCT_KEY, "character", type_="unique")
    op.drop_index(_IX_PRODUCT_KEY, table_name="character")
    op.drop_column("character", "product_key")

    for col in (
        "paused_since",
        "retrying_until",
        "billing_state",
        "is_trial",
        "source",
        "product_id",
        "billing_period",
        "plan",
    ):
        op.drop_column("subscribe", col)
