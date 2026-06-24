# 리팩터링 플랜 — 전역 엔진 → lifespan / app.state

> 목적: 모듈 top의 전역 `engine`/`SessionLocal`을 제거하고, 엔진·세션팩토리를 FastAPI `lifespan`에서 생성해 `app.state`에 보관한다. `get_db`는 `request.app.state`에서 세션팩토리를 꺼낸다. 하이브리드(전역 + DI 혼용) 없이 전면 전환한다.

상태: **승인 대기** · 작성일: 2026-06-24

---

## 0. 전수 검증 결과 (근거)

| 항목 | 수치 |
|---|---|
| 전체 Python 파일 (front/ 제외) | **120개** |
| DB 배선 심볼 참조 파일 | **20개** |
| 그중 실제 코드(주석 제외) | 19개 |
| 실제 수정 대상 | **7개** (핵심 3 + 스크립트 4) |
| 영향 없음(검증됨) | 라우터 12 · service 14 · repository 14 · models 20 · schemas 13 · 테스트 4 · smoke API 8 · alembic |

**핵심 사실**: 모든 service/repository는 `def __init__(self, db: Session)` 생성자 주입으로 세션을 받는다 → 엔진/세션팩토리를 직접 참조하지 않으므로 **이번 변경의 영향이 없다**. 전역을 쓰는 곳은 `db/engine.py`·`db/session.py`(앱), 그리고 앱 바깥 standalone 스크립트뿐.

검증 방법: 전체 120개 `*.py`에 대해 `engine|SessionLocal|get_db|sessionmaker|create_engine|app.state|Base.metadata` grep → 20개 매치 확인 → 각 매치 용도 분류.

---

## 1. 설계 원칙

전역 엔진을 없애되, **앱 바깥 스크립트(seed/inspect/connect)는 `request`가 없어 `app.state`를 쓸 수 없다.** 따라서 "전역 객체"가 아니라 **"팩토리 함수"**를 도입한다:

- `build_engine(settings) -> Engine`
- `build_session_factory(engine) -> sessionmaker`

→ lifespan과 standalone 스크립트가 **각자 명시적으로** 엔진을 만든다. 이로써 하이브리드 없이 일관된다.

안전성(확인됨): `create_engine`은 호출 시 **실제 연결을 열지 않는다**(첫 쿼리 시 lazy 연결). 따라서 lifespan/TestClient 기동 시 DB가 없어도 실패하지 않는다.

---

## 2. 변경 항목 (수정 7개 파일)

### ① `db/engine.py` — 전역 엔진 제거 → 팩토리

```python
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from core.config import Settings


def build_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.DATABASE_URL_POOL,
        poolclass=NullPool,            # pgbouncer 가 풀링 담당
        pool_pre_ping=True,
        echo=(settings.ENV == "dev"),
    )
```
- 삭제: 모듈 top `engine = create_engine(...)`

### ② `db/session.py` — 전역 SessionLocal 제거 → 팩토리 + get_db(request)

```python
from collections.abc import Generator

from fastapi import Request
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine, autocommit=False, autoflush=False, expire_on_commit=False
    )


def get_db(request: Request) -> Generator[Session, None, None]:
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()
```
- 삭제: 모듈 top `SessionLocal = ...`, `from db.engine import engine`
- 변경: `get_db`가 `request: Request`를 받음 (FastAPI가 자동 주입)
- 커밋 전략(명시적 커밋)은 그대로 — `get_db`는 close만 담당

### ③ `main.py` — `create_app()` 팩토리 + `lifespan`

```python
import contextlib
from typing import AsyncIterator

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = app.state.settings          # create_app 이 미리 심어둔 값(이중 해석 방지)
    engine = build_engine(settings)
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)
    try:
        yield
    finally:
        engine.dispose()                   # ★ 종료 시 커넥션 정리


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or default_settings    # 테스트에서 주입 가능
    app = FastAPI(title="BeaverTalk API", version="0.1.0", docs_url="/docs", lifespan=lifespan)
    app.state.settings = settings              # ★ lifespan 이 이걸 읽음
    # CORS / 예외핸들러 / 라우터 4개 / /health / __console 을 그대로 이 안으로 이동
    return app


app = create_app()    # uvicorn main:app 호환 유지
```
- 기존 모듈 레벨 `app = FastAPI(...)`, 미들웨어, 라우터 등록, 예외핸들러, `/health`, `/__console` 을 `create_app()` 내부로 이동
- `from core.config import settings as default_settings`

### ④ `core/deps.py` — 변경 없음(확인)

`from db.session import get_db`, `DbSession = Annotated[Session, Depends(get_db)]`, `get_current_member` 모두 그대로 동작. (`get_db`는 함수로 계속 존재하고, FastAPI가 중첩 의존성의 `Request`를 자동 해결)

### ⑤ standalone 스크립트 4개 — 명시적 엔진 생성으로 교체

| 파일 | 현재 | 변경 후 |
|---|---|---|
| `scripts/seed.py` | `from db.session import SessionLocal` → `SessionLocal()` | `engine = build_engine(settings)` → `SF = build_session_factory(engine)` → `db = SF()` |
| `scripts/inspect_db.py` | `from db.engine import engine` | `engine = build_engine(settings)` |
| `scripts/smoke_connect.py` | `from db.engine import engine` | `engine = build_engine(settings)` |
| `scripts/smoke_infra.py` | engine/SessionLocal/get_db **import 존재** 확인 | `build_engine`/`build_session_factory` 존재 + 생성 가능 확인으로 교체 |

---

## 3. 변경 없음 — 검증 완료 목록

| 대상 | 이유 |
|---|---|
| 라우터 12개 | `DbSession`(=`Depends(get_db)`)로만 주입 — get_db 내부 구현만 바뀜 |
| service 14 · repository 14 | 생성자 `db: Session` 주입 — 엔진/세션 미참조 |
| models 20 · schemas 13 | DB 배선 무관 |
| smoke API 스크립트 8개 | 자체 sqlite 엔진 생성 + `main.app.dependency_overrides[get_db]` — `main.app`·`get_db` 그대로 존재 |
| `smoke_live.py` | 실제 get_db 사용 → 리팩터 후 app.state 엔진을 그대로 탐 |
| `smoke_models.py` | 자체 sqlite 엔진 — 무관 |
| tests/ (4개) | `test_email_verification.py`만 DB 사용(자체 sqlite 픽스처). 나머지는 settings monkeypatch만 — `settings` 전역 유지로 무영향. conftest 없음 |
| `alembic/env.py` | `settings.direct_url`로 자체 엔진(`engine_from_config`) 생성 — 완전 독립 |

---

## 4. 실행 순서

1. `db/engine.py` 팩토리화
2. `db/session.py` 팩토리화 + `get_db(request)`
3. `main.py` `create_app()` + `lifespan` (+ settings를 app.state에 심기)
4. standalone 스크립트 4개 수정
5. 검증:
   - `python -c "import main"` (import 깨짐 확인)
   - `pytest` (회귀)
   - `uvicorn main:app --reload` 기동 + `GET /health` 200 확인

---

## 5. 리스크 / 주의

| 리스크 | 대응 |
|---|---|
| `get_db`에 `Request` 추가로 시그니처 변경 | FastAPI가 자동 주입하므로 라우터 변경 불필요. 단 get_db를 **직접 호출**하는 비-FastAPI 코드가 있으면 깨짐 → 검증 결과 **없음** |
| TestClient 기동 시 lifespan이 실 DB 엔진 생성 | `create_engine`은 연결을 안 열어 안전. smoke 스크립트는 get_db를 override하므로 실 엔진 미사용 |
| `engine.dispose()` 누락 | lifespan `finally`에 포함 |
| settings 이중 해석 | create_app에서 `app.state.settings`에 먼저 심고 lifespan이 그걸 읽음 |

---

## 6. 옵션 (별도 승인 시)

- `conftest.py` 추가: `create_app(test_settings)` + `app.dependency_overrides[get_db]`로 **API 통합테스트** 픽스처 구성 (현재 통합테스트 없음 → smoke 스크립트를 pytest로 승격 가능).
