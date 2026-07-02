# 2장. 설정과 DB 인프라 — Supabase·명시적 커밋

> 📘 **이 장을 읽고 나면**
> - pydantic-settings로 `.env`를 읽고, 운영(prod)에서 기본 JWT 시크릿을 fail-fast로 막는 방식을 설명할 수 있습니다.
> - Supabase의 두 연결(6543 pgbouncer 풀러 vs 5432 직결)을 왜 나눴는지 이해합니다.
> - `NullPool`·`pool_pre_ping`을 왜 쓰는지, 세션 팩토리 옵션(`autocommit/autoflush/expire_on_commit`)의 의미를 압니다.
> - **명시적 커밋 전략**(get_db는 커밋 안 함, Service가 `db.commit()`)이 왜 이 장의 핵심인지 체득합니다.
> - `Base`/`TimestampMixin`/`registry`가 Alembic 마이그레이션과 어떻게 맞물리는지 그릴 수 있습니다.

---

## 2.1 설정 — pydantic-settings와 prod fail-fast 검증

### (1) 왜 필요한가
환경마다(dev/prod) 다른 값(DB URL, 시크릿, API 키)을 코드에 하드코딩하면 사고가 납니다. 특히 **운영에서 개발용 시크릿이 그대로 배포되는 것**이 가장 무서운 사고입니다. 이 프로젝트는 그런 배포를 아예 **기동 단계에서 막습니다**.

### (2) Spring 비유
- `Settings(BaseSettings)` = `@ConfigurationProperties` + `application.yml`/`application-prod.yml`.
- `.env` 파일 로드 = 스프링의 `.properties`/환경변수 바인딩.
- prod 시크릿 검증 실패 시 기동 중단 = 스프링에서 필수 프로퍼티 누락 시 컨텍스트 로딩 실패(`@Validated` + fail-fast).

### (3) 작은 코드 예시
```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    DATABASE_URL_POOL: str            # 필수 (없으면 기동 실패)
    ENV: str = "dev"
    JWT_SECRET: str = _DEV_JWT_SECRET # dev 기본값 편의용

    @model_validator(mode="after")
    def _guard_prod_secret(self):
        if self.ENV == "prod" and self.JWT_SECRET == _DEV_JWT_SECRET:
            raise ValueError("운영에서는 JWT_SECRET 을 반드시 교체해야 합니다.")
        return self
```

### (4) 실제 코드
- `Settings` 클래스와 `.env` 로드 설정: [core/config.py:15](../../core/config.py#L15)
- 필수 런타임 URL(`DATABASE_URL_POOL`)과 선택 마이그레이션 URL: [core/config.py:23](../../core/config.py#L23)
- prod에서 기본 JWT 시크릿이면 기동 차단(fail-fast) 검증기: [core/config.py:88](../../core/config.py#L88)
- import 시점에 `.env`를 로드하는 싱글턴 `settings`: [core/config.py:96](../../core/config.py#L96)

### (5) 흔한 함정
- `.env`에 없는 키를 코드가 요구하면 **앱이 아예 안 뜹니다**(필수 필드). 반대로 `extra="ignore"` 라 `.env`의 여분 키는 조용히 무시됩니다.
- 대부분의 외부 연동 키(SpeechSuper, Resend, Gemini 등)는 `None` 허용이라 없어도 앱은 뜨고 해당 기능만 graceful 폴백합니다. **DB URL과 (prod의) JWT_SECRET만 진짜 필수**라는 점을 구분하세요.

### (6) 한 줄 요약
pydantic-settings가 `.env`를 타입 검증하며 바인딩하고, 운영에서 개발용 시크릿이면 기동 자체를 막습니다.

---

## 2.2 Supabase 이중 연결 — 6543 풀러 vs 5432 직결

### (1) 왜 나눴나
Supabase는 커넥션을 두 경로로 제공합니다.
- **`DATABASE_URL_POOL` (6543, pgbouncer Transaction Pooler)** — 평소 런타임 쿼리용. 짧은 트랜잭션을 대량으로 잘 처리합니다.
- **`DATABASE_URL_DIRECT` (5432, Direct)** — Alembic 마이그레이션용. pgbouncer는 트랜잭션 풀링 모드에서 **DDL/일부 세션 상태에 불안정**하기 때문에, 스키마 변경은 직결로 하는 게 안전합니다.

### (2) Spring 비유
운영 트래픽용 DataSource(HikariCP + 풀러 경유)와, Flyway/Liquibase 마이그레이션 전용 DataSource(직결)를 따로 두는 패턴과 같습니다. 애플리케이션 쿼리와 스키마 관리의 커넥션 경로를 분리하는 것이죠.

### (3) 작은 코드 예시
```python
DATABASE_URL_POOL: str            # 6543 pgbouncer — 런타임
DATABASE_URL_DIRECT: str | None = None  # 5432 — 마이그레이션(선택)

@property
def direct_url(self) -> str:
    """마이그레이션용 URL. 미설정이면 런타임 URL 로 폴백."""
    return self.DATABASE_URL_DIRECT or self.DATABASE_URL_POOL
```

### (4) 실제 코드
- 두 URL 필드와 용도 주석: [core/config.py:22](../../core/config.py#L22)
- `direct_url` 폴백 프로퍼티(DIRECT 미설정 시 POOL 사용): [core/config.py:30](../../core/config.py#L30)
- 파일 상단의 이중 연결 설명 docstring: [core/config.py:1](../../core/config.py#L1)

### (5) 흔한 함정
- **로컬 개발**에서는 `DATABASE_URL_POOL` 하나만 넣으면 됩니다. `direct_url`이 자동으로 POOL로 폴백하기 때문입니다. 굳이 두 개 다 채우지 마세요.
- 반대로 **운영에서 마이그레이션이 이상하게 깨진다면** DIRECT(5432)를 설정했는지 먼저 의심하세요. pgbouncer 경유로 DDL을 돌리면 간헐적으로 실패할 수 있습니다.

### (6) 한 줄 요약
런타임은 6543 풀러(POOL), 마이그레이션은 5432 직결(DIRECT)을 쓰고, DIRECT가 없으면 POOL로 폴백합니다.

---

## 2.3 엔진 — `NullPool`, `pool_pre_ping`, `echo`

### (1) 왜 이렇게 설정하나
Supabase의 6543 뒤에는 이미 **pgbouncer가 커넥션 풀링**을 하고 있습니다. 여기에 SQLAlchemy가 또 풀을 두면 **이중 풀링**이 되어 커넥션이 고갈됩니다. 그래서 SQLAlchemy 쪽 풀은 끄고(`NullPool`) 풀링을 pgbouncer에 전적으로 맡깁니다.

### (2) Spring 비유
- `NullPool` = HikariCP를 끄고 매 요청마다 pgbouncer에서 커넥션을 빌리는 것. 스프링에서는 흔치 않지만, "외부 풀러가 있으니 앱 풀은 최소화" 전략입니다.
- `pool_pre_ping=True` = HikariCP의 `connectionTestQuery`/`keepalive`처럼 죽은(좀비) 커넥션을 쓰기 전에 걸러줍니다.
- `echo=(ENV=="dev")` = 스프링의 `spring.jpa.show-sql=true`(개발 중 SQL 로깅).

### (3) 작은 코드 예시
```python
def build_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.DATABASE_URL_POOL,
        poolclass=NullPool,            # pgbouncer 가 풀링 → SQLA 풀 OFF
        pool_pre_ping=True,            # 죽은 커넥션 자동 감지
        echo=(settings.ENV == "dev"),  # 개발 중 SQL 로깅
    )
```

### (4) 실제 코드
- `build_engine` 팩토리(NullPool + pre_ping + echo): [db/engine.py:24](../../db/engine.py#L24)
- 이중 풀링 방지 이유 설명 docstring: [db/engine.py:1](../../db/engine.py#L1)

### (5) 흔한 함정
- `create_engine`은 **실제 연결을 만들지 않습니다.** 첫 쿼리 시점에 lazy로 연결됩니다. 그래서 이 함수는 비밀번호가 틀려도 성공합니다 — "엔진 생성됐으니 DB 연결 OK"라고 착각하지 마세요.
- 엔진을 전역으로 만들지 않고 `lifespan`에서 만들어 `app.state.engine`에 담습니다(1장 참고). 스크립트(seed/inspect)는 `build_engine`을 직접 부릅니다.

### (6) 한 줄 요약
pgbouncer가 이미 풀링하므로 `NullPool`로 이중 풀링을 막고, `pool_pre_ping`으로 좀비 커넥션을 거릅니다.

---

## 2.4 세션 팩토리와 `get_db`

### (1) 왜 필요한가
요청마다 새 세션을 열고, 끝나면 반드시 닫아야 커넥션이 샙니다. `get_db`가 이 "열고 → 쓰고 → 닫기" 수명을 관리합니다.

### (2) Spring 비유
- `sessionmaker` = `EntityManagerFactory`.
- `get_db` = 요청 스코프의 `EntityManager` 발급 + `finally`에서 정리(스프링은 이걸 컨테이너가 대신 해줍니다).
- `expire_on_commit=False` = 커밋 후에도 엔티티 속성 접근이 가능하게(LazyInitializationException 회피와 비슷한 실용적 선택).

### (3) 작은 코드 예시
```python
def build_session_factory(engine):
    return sessionmaker(
        bind=engine,
        autocommit=False,        # 자동 커밋 안 함
        autoflush=False,         # 쿼리 전 자동 flush 안 함
        expire_on_commit=False,  # 커밋 후에도 ORM 객체 접근 가능
    )

def get_db(request: Request):
    db = request.app.state.session_factory()  # lifespan 이 심어둔 팩토리
    try:
        yield db
    finally:
        db.close()   # ← 커밋은 하지 않음! close 만 한다
```

### (4) 실제 코드
- `build_session_factory`(autocommit/autoflush/expire_on_commit 옵션): [db/session.py:22](../../db/session.py#L22)
- `get_db` — `app.state.session_factory`로 세션 생성 후 `finally` close(커밋 없음): [db/session.py:32](../../db/session.py#L32)
- 라우터에서 `DbSession = Annotated[Session, Depends(get_db)]` 로 주입: [core/deps.py:61](../../core/deps.py#L61)

### (5) 흔한 함정
- `get_db`의 `finally`는 **`close`만** 합니다. 여기엔 자동 커밋도, 자동 롤백-on-exception 로직도 없습니다(예외 발생 시 커밋을 안 했으니 자연히 저장 안 됨 → close로 폐기). 다음 절이 핵심입니다.
- `autoflush=False`라서 "쿼리했더니 알아서 flush돼서 보이겠지"를 기대하면 안 됩니다. 필요하면 명시적으로 `flush()`/`commit()`을 부르세요.

### (6) 한 줄 요약
`get_db`는 세션을 만들고 `finally`에서 닫기만 하며, **커밋은 절대 대신 해주지 않습니다**.

---

## 2.5 ⭐ 명시적 커밋 전략 (이 장에서 가장 중요)

### (1) 왜 이렇게 하나
`get_db`가 커밋하지 않으므로, **저장의 책임은 전적으로 Service에 있습니다.** 쓰기 작업 뒤에 Service가 직접 `self.db.commit()`을 호출해야 실제로 저장됩니다. 트랜잭션 경계가 코드에 눈에 보이게 드러난다는 장점이 있지만, **잊으면 조용히 저장이 안 되는** 단점이 있습니다.

### (2) Spring 비유
Spring `@Transactional` 메서드는 정상 리턴 시 프레임워크가 커밋해 줍니다. 이 프로젝트는 그 자동화를 **일부러 쓰지 않습니다**. 마치 `@Transactional` 없이 `entityManager.getTransaction().commit()`을 매번 손으로 부르는 것과 같습니다.

### (3) 작은 코드 예시
```python
class MemberService:
    def update(self, member_id, data: MemberUpdate) -> Member:
        m = self.repo.get(member_id)
        m.language = data.language
        self.db.commit()   # ← 이 줄이 없으면 close 시 폐기 = 저장 안 됨
        return m
```

### (4) 실제 코드
- Service가 트랜잭션 경계라는 선언(docstring): [domains/account/service/member_service.py:1](../../domains/account/service/member_service.py#L1)
- 실제 명시적 커밋 호출들: [member_service.py:87](../../domains/account/service/member_service.py#L87), [member_service.py:98](../../domains/account/service/member_service.py#L98), [member_service.py:120](../../domains/account/service/member_service.py#L120)
- Repository는 절대 commit 안 함(대비): [domains/account/repository/member_repository.py:1](../../domains/account/repository/member_repository.py#L1)

### (5) 흔한 함정
- **가장 흔한 버그:** "에러도 안 나는데 DB에 반영이 안 돼요" → 십중팔구 Service에서 `commit()`을 빼먹은 것입니다.
- 반대로 **읽기 전용** 메서드에는 커밋을 넣지 마세요. 불필요합니다.
- 커밋은 Service에서만. Repository나 라우터에서 커밋하면 트랜잭션 경계가 흐트러집니다.

### (6) 한 줄 요약
저장하려면 Service가 반드시 `db.commit()`을 호출해야 하며, 이것이 Spring 자동 커밋과의 가장 큰 차이입니다.

---

## 2.6 `Base` / `TimestampMixin` / `registry`와 Alembic

### (1) 왜 필요한가
모든 ORM 모델은 공통 베이스를 상속해야 하고(메타데이터 통합), 생성/수정 시각은 반복되니 믹스인으로 뽑습니다. 그리고 Alembic이 스키마를 인식하려면 **모든 모델이 한 곳에서 import되어 `Base.metadata`에 등록**되어야 합니다.

### (2) Spring 비유
- `Base(DeclarativeBase)` = JPA의 `@MappedSuperclass` 최상위 + 엔티티 메타데이터 컨테이너.
- `TimestampMixin` = `@CreatedDate`/`@LastModifiedDate` (Auditing) 믹스인.
- `registry.py`의 일괄 import = Hibernate가 엔티티를 스캔하도록 패키지를 지정하는 것. 여기서는 **자동 스캔이 없으니 손으로 import**합니다.

### (3) 작은 코드 예시
```python
class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False)
```
```python
# db/registry.py — 이 파일 하나만 import 하면 전 테이블이 metadata 에 등록됨
from domains.account.models.member import Member  # noqa: F401
# ... (도메인이 늘면 여기 import 한 줄만 추가)
```

### (4) 실제 코드
- `Base`(DeclarativeBase) + `TimestampMixin`(server_default now, onupdate): [db/base.py:16](../../db/base.py#L16), [db/base.py:20](../../db/base.py#L20)
- 전 도메인 모델을 한 곳에서 import 하는 레지스트리: [db/registry.py:1](../../db/registry.py#L1)
- 모델이 믹스인을 실제로 상속하는 예: [domains/account/models/member.py:20](../../domains/account/models/member.py#L20)

### (5) 흔한 함정
- **새 모델을 만들었는데 Alembic이 못 잡는다** → `db/registry.py`에 import 한 줄 추가를 빼먹은 것입니다. 이게 마이그레이션 누락의 단골 원인입니다.
- `created_at`/`updated_at`은 `server_default=func.now()`로 **DB 서버 시각**을 씁니다(앱 서버 시각 아님). 타임존은 `timezone=True`로 저장됩니다.

### (6) 한 줄 요약
모든 모델은 `Base`를 상속하고 `registry.py`에 등록되어야 Alembic이 인식하며, 시각 컬럼은 믹스인으로 공유합니다.

---

## 2.7 (배경) lifespan 리팩터 — 전역 엔진 제거

### (1) 왜 바꿨나
예전에는 모듈 top에 전역 `engine`/`SessionLocal`이 있었습니다. 전역은 테스트에서 설정 주입이 어렵고, 앱 수명 관리(종료 시 dispose)가 애매합니다. 그래서 **전역을 없애고 `build_engine`/`build_session_factory` 팩토리 함수를 도입**해, 앱은 `lifespan`에서 만들어 `app.state`에 담고, 스크립트는 직접 호출하도록 전환했습니다.

### (2) 실제 문서/코드
- 리팩터 배경·근거·검증 결과: [REFACTOR_LIFESPAN_PLAN.md](../../REFACTOR_LIFESPAN_PLAN.md)
- 그 결과의 최종 형태(`lifespan`에서 엔진 생성): [main.py:102](../../main.py#L102)

### (3) 한 줄 요약
전역 엔진을 팩토리 + `app.state`로 옮겨, 테스트 주입성과 수명 관리(종료 시 dispose)를 확보했습니다.

---

## ✍️ 스스로 점검
1. Supabase에서 6543(POOL)과 5432(DIRECT)를 나눠 쓰는 이유는 무엇이고, 각각 언제 쓰나요?
2. 엔진에 `NullPool`을 쓰는 이유를 pgbouncer와 연결해 설명해 보세요.
3. `get_db`는 커밋을 하지 않습니다. 그렇다면 데이터가 실제로 저장되게 하려면 누가, 어디서 무엇을 호출해야 하나요?

⟵ [이전: 1장. FastAPI 골격](01-fastapi-vs-spring.md) ・ [📚 목차](README.md) ・ [다음: 목차](README.md) ⟶
