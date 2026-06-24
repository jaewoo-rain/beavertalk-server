"""SQLAlchemy 엔진.

Supabase Transaction Pooler(6543, pgbouncer) 뒤에서 동작하므로
SQLAlchemy 자체 풀은 끄고(NullPool) pgbouncer 가 풀링을 담당하게 한다.
(QueuePool 과 pgbouncer 의 이중 풀링은 연결 고갈을 유발한다.)

주의: create_engine 은 실제 연결을 만들지 않는다. 첫 connect()/쿼리 시점에
연결되므로, 이 모듈 import 자체는 DB 비밀번호가 없어도 성공한다.
"""

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from core.config import settings

engine = create_engine(
    settings.DATABASE_URL_POOL,
    poolclass=NullPool,            # pgbouncer(supabase) 가 풀링 → SQLA 풀 OFF
    pool_pre_ping=True,            # 죽은(좀비) 커넥션 자동 감지
    echo=(settings.ENV == "dev"),  # 개발 중 SQL 로깅 (Spring show-sql 대응)
)
