"""DB 세션 팩토리 및 FastAPI 의존성.

커밋 전략: **명시적 커밋**.
- get_db 는 세션 생성/정리(close)만 담당하고 commit 하지 않는다.
- 쓰기 작업 뒤에는 서비스 계층에서 직접 db.commit() 을 호출한다.
  (Spring @Transactional 의 자동 커밋과 다르니 주의)
"""

from collections.abc import Generator

from sqlalchemy.orm import Session, sessionmaker

from db.engine import engine

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,  # 커밋 후에도 ORM 객체 속성 접근 가능
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 의존성. `db: Session = Depends(get_db)` 로 주입."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
