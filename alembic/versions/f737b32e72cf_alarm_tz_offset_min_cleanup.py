# -*- coding: utf-8 -*-
"""alarm.tz_offset_min 범위 밖 값 정리(§25-①, 출시 전 권장, 선택)

앱 요청서 §25-①. |tz_offset_min| ≥ 1440(24시간) 이면 dispatch_service.
_offset_zone 의 datetime.timezone(timedelta(...))이 ValueError 로 죽는다(파이썬
자체 제약) — 앱은 이 값을 아직 안 보내고 IANA tz 이름만 보낼 예정이라 정상
경로로는 생기지 않지만, 로그인 회원이 API 를 직접 불러 넣을 수는 있었다
(API 레벨 검증은 같은 커밋의 스키마 변경 — domains/alarm/schemas/alarm.py — 이
막는다, 이 마이그레이션은 그 전에 이미 들어와 있을 수 있는 값의 정리다).

⛔ 스키마 자체 CHECK 제약은 걸지 않는다 — NULL(미상)이 정상값이고, 범위만
막으면 충분하다(앱/스크립트가 실수로 큰 값을 또 넣어도 API 레벨 422 가 먼저
막는다). 순수 데이터 정리이므로 모델(character.py 류) 변경은 없다.

Revision ID: f737b32e72cf
Revises: 0c7c15633bc2
"""

from alembic import op

revision = "f737b32e72cf"
down_revision = "0c7c15633bc2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE alarm SET tz_offset_min = NULL "
        "WHERE tz_offset_min IS NOT NULL AND (tz_offset_min < -840 OR tz_offset_min > 840)"
    )


def downgrade() -> None:
    # 정리된 원래 값은 복구할 수 없다(파괴적 정리의 성격상 당연) — no-op.
    pass
