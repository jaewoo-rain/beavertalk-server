"""push_dispatch_log.call_id (통화 캐릭터 서버 결정)

예약전화 발송이 만든 통화 id 를 로그에 남긴다. 통화가 열릴 때 서버가
call_id → push_dispatch_log → alarm → character 로 되짚어 **캐릭터를 스스로
정하기 위한** 열쇠다(그전엔 클라가 start.character_id 로 지정했다).

nullable=True: 이 컬럼 추가 이전에 쌓인 로그가 purge 될 때까지 공존한다.
UNIQUE: 되짚기가 한 행으로 확정되게 한다.

⚠ autogenerate 가 함께 끌어온 무관한 변경(iap_receipt server_default 제거,
learning_item/level/member 의 주석 문구 갱신)은 **의도적으로 뺐다**.
server_default=None 은 NOT NULL 컬럼의 기본값을 떨어뜨리는 실질 변경이고,
주석 드리프트는 이 마이그레이션의 관심사가 아니다.

Revision ID: 3549a9133f9d
Revises: b7e2c5a91d34
Create Date: 2026-07-31 16:54:19.744997

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3549a9133f9d'
down_revision: Union[str, Sequence[str], None] = 'b7e2c5a91d34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UQ = "uq_push_dispatch_log_call_id"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "push_dispatch_log",
        sa.Column(
            "call_id",
            sa.Text(),
            nullable=True,
            comment="발송한 통화 id(uuid4) — 캐릭터 되짚기 열쇠",
        ),
    )
    # 이름을 명시한다 — autogenerate 의 None 은 downgrade 에서 제약을 못 찾는다.
    op.create_unique_constraint(_UQ, "push_dispatch_log", ["call_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(_UQ, "push_dispatch_log", type_="unique")
    op.drop_column("push_dispatch_log", "call_id")
