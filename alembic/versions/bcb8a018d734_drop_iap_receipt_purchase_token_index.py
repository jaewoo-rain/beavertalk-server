# -*- coding: utf-8 -*-
"""iap_receipt.purchase_token 인덱스 — 전체 btree 제거, android 전용 부분 인덱스로
교체(§26, iOS 출시 차단 실사고 + 정정)

⛔⛔ 실사고(2026-09-30, bt-back) — 아이폰 실기기 재시도가 환경 폴백(§26)까지는
성공했는데 그 직후 iap_receipt INSERT 가 500 으로 떨어졌다:

    psycopg2.errors.ProgramLimitExceeded: index row size 5728 exceeds btree
    version 4 maximum 2704 for index "ix_iap_receipt_purchase_token"

iOS purchase_token 은 StoreKit2 JWS(약 5.7KB)인데 그 컬럼 **전체**에 btree
인덱스가 걸려 있어 PostgreSQL 행 크기 한도(2704B)를 넘었다 — iOS 영수증을
한 건도 저장할 수 없는 상태였다. Android 토큰(최대 144자)은 한도 안이라
통과했었다.

⚠ 이 인덱스는 §24(2026-09-28) 때 "purchase_token + 인덱스(RTDN 이 토큰으로
찾아온다)"로 만들었다 — bt-back 이 검토·승인했다. iOS 토큰 길이를 못 봤다.

첫 정정(단순 drop)에 대해 "나중에 조회 안 하냐"는 지적이 맞았다 — 다만 실제로
필요한 조회는 **android 하나뿐**이다(실측):

  | 알림          | 무엇을 들고 오나         | 무엇으로 찾나        | 인덱스 필요 |
  |---------------|--------------------------|----------------------|-------------|
  | Play RTDN     | purchaseToken            | purchase_token       | 필요        |
  | Apple ASSN V2 | originalTransactionId    | transaction_id(이미 uq_iap_platform_tx 가 인덱스) | 불필요 |

⇒ 전체 인덱스를 **android 행만의 부분 인덱스**로 교체한다 — iOS 행은 인덱스에
아예 안 들어가므로 5.7KB JWS 가 btree 한도를 넘을 일이 없고, Play RTDN 이 쓸
`WHERE platform='android' AND purchase_token=?` 조회는 정확히 받는다.

⛔ UNIQUE 로 만들지 않는다 — ①복원은 같은 토큰을 다시 보낸다 ②Play 구독은
갱신해도 purchaseToken 이 그대로라(orderId 만 갱신마다 바뀐다) 같은 토큰이
여러 iap_receipt 행에 붙을 수 있다 — UNIQUE 면 그 순간 IntegrityError 다.

⛔ 해시 컬럼(purchase_token_hash)은 만들지 않는다 — 이 부분 인덱스가 같은
일을 하고 쓰기 경로도 안 늘린다.

Revision ID: bcb8a018d734
Revises: a5b1e6ca8a67
"""

import sqlalchemy as sa
from alembic import op

revision = "bcb8a018d734"
down_revision = "a5b1e6ca8a67"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_iap_receipt_purchase_token", table_name="iap_receipt")
    op.create_index(
        "ix_iap_receipt_purchase_token_android", "iap_receipt", ["purchase_token"],
        postgresql_where=sa.text("platform = 'android'"),
    )


def downgrade() -> None:
    op.drop_index("ix_iap_receipt_purchase_token_android", table_name="iap_receipt")
    # ⛔⛔ 옛 전체 컬럼 인덱스는 되만들지 않는다 — 되만들면 같은 사고(iOS JWS
    # 5.7KB > btree 2704B 한도)가 재발한다(위 docstring 실사고 참조).
