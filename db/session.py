"""DB 세션 팩토리 및 FastAPI 의존성.

세션 팩토리는 전역으로 만들지 않고 `build_session_factory(engine)` 으로 생성한다.
- 앱: main.py 의 lifespan 이 호출해 app.state.session_factory 에 보관
- get_db 는 그 팩토리를 request.app.state 에서 꺼낸다(전역 의존 없음)

커밋 전략: **명시적 커밋**.
- get_db 는 세션 생성/정리(close)만 담당하고 commit 하지 않는다.
- 쓰기 작업 뒤에는 서비스 계층에서 직접 db.commit() 을 호출한다.
  (Spring @Transactional 의 자동 커밋과 다르니 주의)
"""

from __future__ import annotations

from collections.abc import Generator

from fastapi import Request
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """엔진에 바인딩된 세션 팩토리를 생성한다."""
    return sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,  # 커밋 후에도 ORM 객체 속성 접근 가능
    )


def get_db(request: Request) -> Generator[Session, None, None]:
    """FastAPI 의존성. lifespan 이 app.state 에 심어둔 세션 팩토리를 사용한다."""
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()
