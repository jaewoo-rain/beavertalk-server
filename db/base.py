"""SQLAlchemy 공통 베이스 및 믹스인.

- `Base`            : 모든 ORM 모델이 상속하는 DeclarativeBase
- `TimestampMixin`  : created_at / updated_at 타임스탬프 컬럼 제공

SQLite·PostgreSQL 양쪽에서 동작하도록 `func.now()`(=DB 서버 시각)를
server_default 로 사용한다.
"""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """모든 모델의 베이스 클래스."""


class TimestampMixin:
    """생성/수정 시각을 자동으로 관리하는 믹스인.

    - ``created_at`` : INSERT 시 DB 서버 시각으로 자동 설정 (이후 불변)
    - ``updated_at`` : INSERT 시 설정되고, UPDATE 시마다 갱신
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="생성 시각",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="수정 시각",
    )
