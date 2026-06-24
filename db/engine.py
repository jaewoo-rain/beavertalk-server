"""SQLAlchemy 엔진 팩토리.

Supabase Transaction Pooler(6543, pgbouncer) 뒤에서 동작하므로
SQLAlchemy 자체 풀은 끄고(NullPool) pgbouncer 가 풀링을 담당하게 한다.
(QueuePool 과 pgbouncer 의 이중 풀링은 연결 고갈을 유발한다.)

엔진은 전역으로 만들지 않고 `build_engine(settings)` 으로 생성한다.
- 앱: main.py 의 lifespan 이 호출해 app.state.engine 에 보관
- 스크립트(seed/inspect/connect): 각자 명시적으로 호출

주의: create_engine 은 실제 연결을 만들지 않는다. 첫 connect()/쿼리 시점에
연결되므로, 이 함수 호출 자체는 DB 비밀번호가 없어도 성공한다.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from core.config import Settings


def build_engine(settings: Settings) -> Engine:
    """설정으로부터 엔진을 생성한다(연결은 첫 쿼리 때 lazy)."""
    return create_engine(
        settings.DATABASE_URL_POOL,
        poolclass=NullPool,            # pgbouncer(supabase) 가 풀링 → SQLA 풀 OFF
        pool_pre_ping=True,            # 죽은(좀비) 커넥션 자동 감지
        echo=(settings.ENV == "dev"),  # 개발 중 SQL 로깅 (Spring show-sql 대응)
    )
