# BeaverTalk DB 구축 플랜 (FastAPI + SQLAlchemy 2.0 + Supabase/PostgreSQL)

> 작성: db-architect + fastapi-architect 에이전트 설계 통합
> 대상: `c:\Users\jaewoo\Desktop\coding\fastapi\SQLAlchemy`
> 전제: `models/base.py`(Base + TimestampMixin) 이미 생성됨. ERD = `BeaverTalk.vuerd.json`

---

## 0. 핵심 결정 요약 (TL;DR)

| 항목 | 결정 | 이유 |
|---|---|---|
| 드라이버 | **sync (psycopg2-binary)** | 초보자, Supabase pgbouncer + asyncpg의 prepared statement 충돌 회피 |
| 런타임 연결 | **6543 Transaction Pooler** + `NullPool` | pgbouncer가 풀링 → SQLAlchemy 풀 OFF (이중풀링 방지) |
| 마이그레이션 연결 | **5432 Direct/Session** | DDL은 풀러 우회, 전체 PG 기능 |
| 마이그레이션 도구 | **Alembic** (autogenerate) | Flyway/Liquibase 대응. `ddl-auto` 같은 자동변경 금지 |
| 금액 타입 | **Numeric(10,2)** (Decimal) | float 금지 (ERD의 DATETIME 오타 수정) |
| PK | **BigInteger + Identity()** | JPA IDENTITY 대응, 대량 테이블 대비 |

### 사용자 결정 (확정 ✅)
1. **member ↔ speak_country 관계** → **1:1** (회원당 억양 1건. speak_country.member_id = UNIQUE FK)
2. **member.character_id(대표 캐릭터)** → **유지** (user_character와 별개 FK이므로 relationship에 `foreign_keys=[Member.character_id]` 명시 필요)
3. **커밋 패턴** → **명시적 커밋** (서비스 계층에서 `db.commit()` 직접 호출. `get_db`는 close만 담당)

---

## 1. JPA → SQLAlchemy 2.0 개념 대응표

| JPA (Spring) | SQLAlchemy 2.0 |
|---|---|
| `@Entity`, `@Table(name=)` | `class X(Base): __tablename__ = "x"` |
| `@Id @GeneratedValue(IDENTITY)` | `mapped_column(BigInteger, Identity(), primary_key=True)` |
| `@Column(nullable=false)` | `Mapped[str]` (non-Optional → 자동 NOT NULL) |
| `@Column(nullable=true)` | `Mapped[Optional[str]]` |
| `@Column(unique=true)` | `mapped_column(unique=True)` / 복합은 `UniqueConstraint` |
| `@ManyToOne` | 자식: `Mapped["Parent"] = relationship(...)` + `ForeignKey` |
| `@OneToMany(mappedBy="x")` | 부모: `Mapped[list["Child"]] = relationship(back_populates="x")` |
| `mappedBy="x"` | `back_populates="x"` (**양쪽 다** 명시) |
| `@JoinColumn(name=)` | `mapped_column(ForeignKey("parent.id"))` (FK는 **자식**에만) |
| `@OneToOne` | `Mapped["Child"] = relationship(...)` + 자식 FK에 `unique=True` |
| `fetch = LAZY` | `relationship(lazy="select")` ← SQLA 기본값 |
| `fetch = EAGER` | 컬렉션 `lazy="selectin"` / 스칼라 `lazy="joined"` |
| `cascade = ALL` | `cascade="all"` |
| `cascade = REMOVE` | `cascade="delete"` |
| `orphanRemoval = true` | `cascade="all, delete-orphan"` |
| FK `ON DELETE CASCADE` (DB) | `ForeignKey(..., ondelete="CASCADE")` + `relationship(passive_deletes=True)` |
| `@CreationTimestamp` | `server_default=func.now()` (TimestampMixin) |
| Repository / EntityManager | `Session` (`session.get`, `session.scalars(select(...))`) |

**JPA 경험자 3대 함정**
1. `cascade`(ORM 세션 레벨) ≠ `ondelete`(DB 레벨). 같이 쓸 땐 `passive_deletes=True`.
2. `back_populates`는 **양쪽 다** 적는다 (JPA `mappedBy`는 한쪽만).
3. SQLA 기본 lazy=`select` → 컬렉션 접근마다 쿼리 = **N+1**. 쿼리에서 `selectinload()`로 해결.

---

## 2. 관계 패턴별 코드 템플릿

### A. 1:N 양방향 (member 1—N call)
```python
class Member(Base):
    calls: Mapped[list["Call"]] = relationship(
        back_populates="member", cascade="all, delete-orphan",
        passive_deletes=True, lazy="select")   # 목록은 쿼리에서 selectinload

class Call(Base):
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE"), index=True)
    member: Mapped["Member"] = relationship(back_populates="calls", lazy="joined")
```
> 컬렉션은 `selectin`(IN절 2차쿼리), 스칼라(자식→부모)는 `joined`(JOIN 1번). 컬렉션을 joined로 하면 카테시안 폭발.

### B. N:1 단방향 (call → character, 캐릭터는 통화목록 모름)
```python
class Call(Base):
    character_id: Mapped[int] = mapped_column(
        ForeignKey("character.character_id", ondelete="RESTRICT"), index=True)
    character: Mapped["Character"] = relationship(lazy="joined")  # back_populates 없음
```
> 마스터 데이터(character)는 참조당하면 삭제 금지(RESTRICT).

### C. 1:1 (sentence — evaluation)
```python
class Sentence(Base):
    evaluation: Mapped[Optional["Evaluation"]] = relationship(
        back_populates="sentence", cascade="all, delete-orphan",
        uselist=False, lazy="joined")

class Evaluation(Base):
    sentence_id: Mapped[int] = mapped_column(
        ForeignKey("sentence.sentence_id", ondelete="CASCADE"),
        unique=True)        # ← 이 UNIQUE가 "1건당 1건"을 DB에서 보장
    sentence: Mapped["Sentence"] = relationship(back_populates="evaluation")
```

### D. N:M + 추가컬럼 = Association Object (member — character via user_character)
`purchase_price/date` 추가컬럼이 있어 `secondary=` 가 아니라 **명시적 매핑 클래스** 사용.
```python
class UserCharacter(Base):
    __tablename__ = "user_character"
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE"), primary_key=True)
    character_id: Mapped[int] = mapped_column(
        ForeignKey("character.character_id", ondelete="RESTRICT"), primary_key=True)
    purchase_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    purchase_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    member: Mapped["Member"] = relationship(back_populates="owned_characters")
    character: Mapped["Character"] = relationship(back_populates="owners")
```
> **복합 PK (member_id, character_id)** = "같은 캐릭터 중복구매 불가" 보장.

---

## 3. 테이블별 관계 매핑 (16개 관계)

| 부모 | 자식/상대 | 종류 | 방향 | FK 보유 | back_populates | lazy | cascade | ON DELETE |
|---|---|---|---|---|---|---|---|---|
| member | speak_country | 1:1 | 양방향 | speak_country | speak_country↔member | joined | all,delete-orphan | CASCADE |
| member | alarm | 1:N | 양방향 | alarm | alarms↔member | select | all,delete-orphan | CASCADE |
| member | subscribe | 1:N | 양방향 | subscribe | subscribes↔member | select | all,delete-orphan | CASCADE |
| member | payment | 1:N | 양방향 | payment | payments↔member | select | all,delete-orphan | CASCADE |
| member | call | 1:N | 양방향 | call | calls↔member | select | all,delete-orphan | CASCADE |
| member | user_character | 1:N | 양방향 | user_character | owned_characters↔member | selectin | all,delete-orphan | CASCADE |
| member | character(대표) | N:1 | 단방향 | member.character_id | (없음) | joined | none | SET NULL |
| character | user_character | 1:N | 양방향 | user_character | owners↔character | select | none | RESTRICT |
| character | discount_event | 1:N | 양방향 | discount_event | discount_events↔character | select | all,delete-orphan | CASCADE |
| character | alarm | N:1 | 단방향 | alarm.character_id | (alarm→char만) | joined | none | RESTRICT |
| character | call | N:1 | 단방향 | call.character_id | (call→char만) | joined | none | RESTRICT |
| alarm | schedule | 1:N | 양방향 | schedule | schedules↔alarm | selectin | all,delete-orphan | CASCADE |
| call | call_raw_data | 1:N | 양방향 | call_raw_data | raw_data↔call | select | all,delete-orphan | CASCADE |
| call | sentence | 1:N | 양방향 | sentence | sentences↔call | select | all,delete-orphan | CASCADE |
| sentence | evaluation | 1:1 | 양방향 | evaluation | evaluation↔sentence | joined | all,delete-orphan | CASCADE |
| sentence | review | 1:1 | 양방향 | review | review↔sentence | select | all,delete-orphan | CASCADE |

**방향 원칙**: 마스터 데이터(character)로의 참조 = 단방향 N:1 + RESTRICT / 소유 하위데이터 = 양방향 1:N + CASCADE.
**주의**: member→character FK가 2개(대표캐릭터 + user_character)라 relationship에 `foreign_keys=[...]` 명시 필요.

---

## 4. 제약 · 인덱스

**UNIQUE**: `member.email` / `member.(login_method, unique_value)` 복합 / `evaluation.sentence_id` / `speak_country.member_id` / `review.sentence_id`(PK겸FK) / `user_character.(member_id, character_id)`(복합PK)

**FK 인덱스** (PG는 FK 자동 인덱스 X → 전부 `index=True`):
alarm.member_id·character_id, schedule.alarm_id, subscribe.member_id, payment.member_id, call.member_id·character_id, call_raw_data.call_id, sentence.call_id, member.character_id, discount_event.character_id

**조회용 복합/부분 인덱스**:
- `ix_call_member_date (member_id, call_date)` — 내 통화 최신순
- `ix_sentence_call (call_id)` — 통화별 발화
- 활성 할인행사 부분인덱스 `WHERE activate`

**CHECK** (Alembic 수동 추가): `rating BETWEEN 1 AND 3`, `percent BETWEEN 0 AND 100`

---

## 5. ERD 수정 반영 사항 (구현 시 적용)

| 항목 | 원본 | 수정 |
|---|---|---|
| 테이블명 케이스 | DiscountEvent/Evaluation 등 | 전부 snake_case |
| 금액 타입 | DATETIME 오타 / FLOAT | Numeric(10,2) |
| 오타 | void_url / speek / evalution / discription / recorde | voice_url / speak / evaluation / description / recorded |
| 양방향 중복 FK 3건 | member↔speak_country, character↔discount_event, sentence↔evaluation | 각 방향 1개만 |
| member.notification_id | alarm 역참조(잘못) | 제거 (alarm.member_id 채택) |
| password | 평문 | password_hash (해시 저장) |
| user_character PK | 없음 | 복합 PK |

---

## 6. 프로젝트 구조 (루트 models/ 유지안)

```
SQLAlchemy/
├─ models/                 # 테이블 1개 = 파일 1개 (ERD와 1:1)
│  ├─ base.py              # ✅ 생성됨: Base + TimestampMixin
│  ├─ __init__.py          # ✅ 생성됨: 전 모델 re-export (Alembic 집결지)
│  ├─ member.py  speak_country.py  alarm.py  schedule.py
│  ├─ character.py  user_character.py  discount_event.py
│  ├─ payment.py  subscribe.py  call.py  call_raw_data.py
│  └─ sentence.py  evaluation.py  review.py
├─ db/
│  ├─ engine.py            # create_engine(NullPool, pool_pre_ping)
│  └─ session.py           # SessionLocal + get_db (Depends)
├─ core/
│  └─ config.py            # pydantic-settings (DATABASE_URL_POOL/DIRECT)
├─ alembic/                # alembic init (env.py에서 Base.metadata 연결)
├─ alembic.ini
├─ main.py                 # FastAPI 앱 + 라우터
├─ .env / .env.example     # gitignore
└─ requirements.txt
```
> Spring 대응: `models/`=@Entity, `db/`=DataSource/EntityManager, `core/config`=application.yml, `alembic/`=Flyway. **JPA Repository 자동생성 없음** → CRUD 함수 직접 작성. **Entity ≠ DTO** → 응답은 Pydantic schema로 분리.

---

## 7. 세션/엔진 (sync + Supabase)

```python
# core/config.py
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    DATABASE_URL_POOL: str    # 6543 런타임
    DATABASE_URL_DIRECT: str  # 5432 마이그레이션
    ENV: str = "dev"
settings = Settings()

# db/engine.py
engine = create_engine(
    settings.DATABASE_URL_POOL,
    poolclass=NullPool,       # ★ pgbouncer가 풀링하므로 SQLA 풀 OFF
    pool_pre_ping=True,       # 죽은 커넥션 자동 감지
    echo=(settings.ENV == "dev"))

# db/session.py
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()
```
`.env` (Supabase 대시보드 → Connect):
```
DATABASE_URL_POOL=postgresql+psycopg2://postgres.<ref>:<PWD>@aws-0-<region>.pooler.supabase.com:6543/postgres
DATABASE_URL_DIRECT=postgresql+psycopg2://postgres.<ref>:<PWD>@aws-0-<region>.pooler.supabase.com:5432/postgres
```
> 사용자명은 `postgres.<project-ref>`(점 포함). 비밀번호 특수문자는 URL 인코딩(`@`→`%40`).

**트랜잭션 경계 (vs Spring @Transactional)**: SQLAlchemy는 자동 커밋 없음 → 쓰기 후 **`db.commit()` 직접 호출**(권장: 서비스 계층 명시적 커밋).

---

## 8. Alembic 마이그레이션

```python
# alembic/env.py 핵심 수정
from core.config import settings
from models import Base                       # 전 모델 등록된 Base
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL_DIRECT)  # 5432!
target_metadata = Base.metadata
# context.configure(..., compare_type=True, compare_server_default=True)
```
명령:
```bash
alembic init alembic
alembic revision --autogenerate -m "initial 14 tables"
# → 생성 스크립트 검토 (FK 순서, 인덱스, CHECK, enum 누락 보완)
alembic upgrade head
```
> 함정: 모델이 `models/__init__.py`에서 import 안 되면 Alembic이 못 봄 → 빈 마이그레이션.

---

## 9. 설치 패키지 (requirements.txt)

```
sqlalchemy>=2.0,<2.1
alembic>=1.13
psycopg2-binary>=2.9
fastapi>=0.110
uvicorn[standard]>=0.29
pydantic>=2.6
pydantic-settings>=2.2
python-dotenv>=1.0
```
```bash
conda activate beavertalk-server && pip install -r requirements.txt
```

---

## 10. 구축 단계 체크리스트 (진행 상황)

```
[x] 1. requirements.txt 작성 → pip install (beavertalk-server 환경)
[ ] 2. Supabase Connect에서 6543/5432 연결문자열 복사   ← 비밀번호 대기 중
[~] 3. .env 작성 (플레이스홀더) + .gitignore + core/config.py   (실값만 교체하면 됨)
[x] 4. db/engine.py, db/session.py
[x] 5a. 인프라 스모크 (scripts/smoke_infra.py) — dialect/NullPool/설정 로드 OK
[ ] 5b. 실제 연결 스모크 (scripts/smoke_connect.py)   ← 비밀번호 대기 중
[x] 6. models/*.py 14개 작성 (§2 패턴 + §3 매핑표)
[x] 7. models/__init__.py 에 14개 전부 import
[x] 7b. 모델 매핑 검증 (scripts/smoke_models.py) — configure_mappers + sqlite create_all OK
[x] 8. alembic init → env.py 수정 (Base.metadata, DIRECT URL, compare_type)
[ ] 9. alembic revision --autogenerate -m "initial 14 tables" → 스크립트 검토   ← 비밀번호 대기 중
[ ] 10. alembic upgrade head → Supabase Table Editor 확인   ← 비밀번호 대기 중
[ ] 11. main.py + 라우터 1개로 get_db 동작 확인
```

### 비밀번호 받은 뒤 실행 순서
```bash
# 1) .env 의 PLACEHOLDER 를 실제 Supabase 값으로 교체 (POOL=6543, DIRECT=5432)
# 2) 연결 확인
python scripts/smoke_connect.py
# 3) 초기 마이그레이션 생성 → 생성된 alembic/versions/*.py 검토
python -m alembic revision --autogenerate -m "initial 14 tables"
# 4) Supabase 에 적용
python -m alembic upgrade head
```
> autogenerate 검토 포인트: FK 생성 순서, CHECK 제약(rating 1~3 등은 수동 추가), Identity 컬럼, 부분 인덱스.

---

## 부록: 참고 출처
- SQLAlchemy 2.0 relationships: https://docs.sqlalchemy.org/en/20/orm/basic_relationships.html
- Supabase 연결 포트(6543/5432): https://supabase.com/docs/guides/database/connecting-to-postgres
- asyncpg+pgbouncer 충돌: https://github.com/supabase/supabase/issues/39227