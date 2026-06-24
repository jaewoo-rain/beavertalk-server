import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# 프로젝트 루트를 import 경로에 추가 (alembic/ 의 부모 = 프로젝트 루트)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import settings  # noqa: E402
from db.registry import Base  # noqa: E402  (전 도메인 모델 등록 + Base)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# 마이그레이션은 5432 Direct 연결 사용 (pgbouncer 우회). .env 에서 주입.
config.set_main_option("sqlalchemy.url", settings.direct_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 가 비교할 대상 = 우리 모델 메타데이터
target_metadata = Base.metadata

# ⚠️ Alembic 이 '우리 모델에 정의된 테이블'만 관리하도록 제한.
# 이렇게 하면 모델에 없는 기존 테이블(예: waitlist)을 autogenerate 가 DROP 하지 않는다.
_OWNED_TABLES = set(Base.metadata.tables.keys())


def include_name(name, type_, parent_names):
    if type_ == "table":
        return name in _OWNED_TABLES
    return True

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,            # 컬럼 타입 변경 감지
            compare_server_default=True,  # server_default 변경 감지
            include_name=include_name,    # 우리 테이블만 관리(waitlist 등 보호)
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
