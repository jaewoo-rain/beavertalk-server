# 4장. Alembic 스키마 마이그레이션

> 📘 **이 장을 읽고 나면**
> - Alembic 이 "DB 스키마의 Git" 이라는 개념과, 모델 코드(파이썬)와 실제 DB 스키마가 서로 다른 것이라는 사실을 구분할 수 있어요.
> - `alembic upgrade head` 로 빈 DB 를 통째로 세팅하는 흐름과, autogenerate 워크플로(모델 수정 → revision → 검토 → upgrade)를 따라 할 수 있어요.
> - 왜 `Base.metadata.create_all()` 로 우회하면 안 되는지 설명할 수 있어요.
> - `alembic/env.py` 가 어떻게 14개 테이블을 인식하고, 왜 6543 이 아니라 5432(direct)로 붙으며, `include_name` 으로 "우리 테이블만" 건드리는지 이해할 수 있어요.
> - Spring 의 Flyway/Liquibase 와 1:1로 매핑해서 이해할 수 있어요.

---

## Alembic 이 뭔가요? — DB 스키마의 버전 관리

왜 필요하냐면, **모델 코드를 고쳐도 실제 데이터베이스는 저절로 바뀌지 않기** 때문이에요. 파이썬 클래스(`Member`)에 컬럼 하나를 추가한다고 해서 운영 DB 의 `member` 테이블에 컬럼이 생기지 않습니다. 이 둘은 별개예요.

- **모델 코드** = "테이블이 이렇게 생겼으면 좋겠다" 는 우리의 선언 (SQLAlchemy 모델).
- **실제 DB 스키마** = 지금 Postgres 안에 진짜로 존재하는 테이블 구조.

Alembic 은 이 간극을 메우는 도구입니다. "테이블을 어떻게 바꿔왔는지" 를 순서 있는 파일(`alembic/versions/*.py`)로 기록하고 재생해요. **Git 이 코드 이력을 관리하듯, Alembic 은 스키마 이력을 관리**합니다.

### Spring 비유: Flyway / Liquibase

Spring 개발자에겐 익숙한 그림이에요.

| Spring(Flyway) | BeaverTalk(Alembic) |
|---|---|
| `V1__init.sql`, `V2__add_col.sql` | `alembic/versions/8390e67d05bd_*.py`, `b1c2d3e4f5a6_*.py` |
| `flyway_schema_history` 테이블 | `alembic_version` 테이블 |
| `flyway migrate` | `alembic upgrade head` |
| `ddl-auto: none` (자동 생성 끔) | `create_all()` 안 쓰고 마이그레이션만 사용 |

Flyway 를 써봤다면 개념은 그대로입니다. 다른 점은 Alembic 은 **모델을 보고 마이그레이션 초안을 자동 생성(autogenerate)** 해준다는 것 정도예요(Flyway 는 SQL 을 직접 씁니다).

> 한 줄 요약: Alembic 은 "스키마용 Git" 이고, Spring 의 Flyway/Liquibase 자리에 해당합니다.

---

## 빈 DB 를 통째로 세팅 — `alembic upgrade head`

새 환경(팀원의 로컬, 새 Supabase 프로젝트)에서 DB 를 처음 세팅할 때는 이 한 줄이면 됩니다.

```bash
alembic upgrade head
```

이 명령은 `alembic/versions/` 의 마이그레이션을 **첫 번째부터 최신(head)까지 순서대로 재생**해 스키마를 통째로 만들어줍니다. BeaverTalk 에는 현재 두 개의 마이그레이션이 사슬로 연결돼 있어요.

```
(빈 DB)
   │  8390e67d05bd  initial schema (supabase auth)  ← 14개 테이블 전부 생성
   ▼
   │  b1c2d3e4f5a6  member soft delete (deleted_at)  ← member 에 컬럼 추가
   ▼
 (head = 최신)
```

두 번째 마이그레이션은 첫 번째를 `down_revision` 으로 가리켜 순서를 고정합니다.

실제 코드 링크:
- [alembic/versions/8390e67d05bd_initial_schema_supabase_auth.py:21](../../alembic/versions/8390e67d05bd_initial_schema_supabase_auth.py#L21) — 초기 스키마 `upgrade()`(level, speak_country, voice … 순서대로 `create_table`).
- [alembic/versions/b1c2d3e4f5a6_member_soft_delete.py:20](../../alembic/versions/b1c2d3e4f5a6_member_soft_delete.py#L20) — `down_revision = '8390e67d05bd'`(사슬 연결).
- [alembic/versions/b1c2d3e4f5a6_member_soft_delete.py:25](../../alembic/versions/b1c2d3e4f5a6_member_soft_delete.py#L25) — `deleted_at` 컬럼·인덱스 추가.
- [README.md:70](../../README.md#L70) — README 2장의 `alembic upgrade head` 설명.

### 흔한 함정: `Base.metadata.create_all()` 로 우회 금지

빠르니까 이걸로 테이블을 만들고 싶은 유혹이 생깁니다.

```python
Base.metadata.create_all(engine)   # ❌ 하지 마세요
```

이게 왜 문제냐면:

1. **빈 DB 엔 테이블을 만들어주지만, 기존 테이블의 컬럼 변경은 못 합니다.** 나중에 컬럼을 바꿔야 할 때 손도 못 대요.
2. **`alembic_version` 테이블이 안 생깁니다.** Alembic 은 "지금 DB 가 몇 번 버전인지" 를 이 테이블로 추적하는데, `create_all` 로 만들면 버전 추적이 비어 있어 이후 `alembic upgrade` 가 충돌합니다("테이블이 이미 있다" 류 에러).

Spring 으로 치면 `ddl-auto: update` 로 대충 굴리다가 운영에서 스키마가 꼬이는 것과 같은 함정이에요. **셋업은 항상 `alembic upgrade head`.**

> 한 줄 요약: 새 DB 는 `alembic upgrade head` 로 세팅하고, `create_all()` 우회는 버전 추적을 깨뜨리니 절대 쓰지 마세요.

---

## autogenerate 워크플로 — 모델 바꾸고 마이그레이션 만들기

왜 필요하냐면, 모델에 컬럼을 추가한 뒤 그 변경을 DB 에 반영할 마이그레이션 파일이 있어야 다른 사람·운영 환경도 같은 변경을 재현할 수 있기 때문이에요. Alembic 은 이걸 손으로 다 쓰게 하지 않고, **모델과 현재 DB 를 비교해 초안을 자동 생성**해줍니다.

정석 순서는 이렇습니다.

```bash
# 1) 모델 코드를 먼저 수정한다 (예: Member 에 nickname 컬럼 추가)
# 2) 변경을 감지해 마이그레이션 초안 생성
alembic revision --autogenerate -m "member: add nickname"
# 3) 생성된 alembic/versions/xxxx.py 를 눈으로 검토한다 (autogenerate 는 100% 가 아님!)
# 4) 적용
alembic upgrade head
```

그리고 **모델 변경 + 마이그레이션 파일을 같은 커밋에 함께 넣습니다.** 그래야 코드와 스키마 이력이 한 몸으로 움직여요.

### 작은 코드 예시 — 자동 생성이 만들어주는 것

`b1c2d3e4f5a6` 마이그레이션이 딱 이 워크플로의 결과물입니다. 모델에 `deleted_at` 을 추가했더니 autogenerate 가 이런 `upgrade()` 를 만들어줬어요.

```python
def upgrade() -> None:
    op.add_column('member',
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True,
                  comment='탈퇴 시각(소프트 삭제, NULL=활성)'))
    op.create_index(op.f('ix_member_deleted_at'), 'member', ['deleted_at'], unique=False)
```

실제 코드 링크:
- [README.md:89](../../README.md#L89) — autogenerate 4단계 워크플로 원문.
- [alembic/versions/b1c2d3e4f5a6_member_soft_delete.py:25](../../alembic/versions/b1c2d3e4f5a6_member_soft_delete.py#L25) — autogenerate 산출물 예시.

### 흔한 함정

- **autogenerate 를 맹신하지 마세요.** 서버 기본값(`server_default`), 데이터 이관, 이름 변경(rename) 같은 건 못 잡거나 잘못 잡습니다. 반드시 생성된 파일을 읽고 손봐야 해요("please adjust!" 주석이 괜히 있는 게 아닙니다).
- **모델만 바꾸고 마이그레이션을 안 만드는 실수.** 그러면 다음 사람이 `column "..." does not exist` 에러를 만납니다([README.md:171](../../README.md#L171) 트러블슈팅 표 참고).

> 한 줄 요약: 모델 수정 → `revision --autogenerate` → 검토 → `upgrade head` 를 한 커밋으로. 자동 생성은 초안일 뿐 항상 검토하세요.

---

## `alembic/env.py` — 마이그레이션의 뇌

이 파일이 "Alembic 이 무엇을, 어디에, 어떻게" 적용할지 결정합니다. 세 가지 포인트만 알면 충분해요.

### 1) 14개 테이블을 어떻게 다 인식하나 — `db.registry` 의 Base import

autogenerate 가 비교하려면 우리 모델 전부가 `Base.metadata` 에 등록돼 있어야 합니다. 그런데 모델 파일은 도메인별로 흩어져 있어요. 그래서 `db/registry.py` 가 **모든 도메인 모델을 한 번씩 import** 해 등록을 강제하고, `env.py` 는 이 레지스트리 하나만 import 하면 14개 테이블을 전부 봅니다.

```python
# alembic/env.py
from db.registry import Base            # ← 이 import 하나가 14개 테이블 전부 등록
target_metadata = Base.metadata         # autogenerate 비교 대상
```

실제 코드 링크:
- [alembic/env.py:14](../../alembic/env.py#L14) — `from db.registry import Base`.
- [alembic/env.py:29](../../alembic/env.py#L29) — `target_metadata = Base.metadata`.
- [db/registry.py:10](../../db/registry.py#L10) — account/commerce/learning/alarm 전 도메인 모델 import("도메인 늘면 여기 한 줄 추가").

### 2) 왜 6543 이 아니라 5432(direct)로 붙나

BeaverTalk 은 Supabase Postgres 를 두 경로로 씁니다.

- **런타임(앱)**: 6543 **Transaction Pooler**(pgbouncer) — 커넥션을 많이 여닫는 웹 트래픽용.
- **마이그레이션**: 5432 **Direct 연결** — pgbouncer 를 우회.

DDL(테이블 생성·변경) 같은 세션 상태가 필요한 작업은 pgbouncer 의 transaction 모드와 궁합이 안 맞습니다. 그래서 `env.py` 는 일부러 `settings.direct_url`(5432)을 씁니다.

```python
# alembic/env.py
config.set_main_option("sqlalchemy.url", settings.direct_url)  # 마이그레이션은 5432 Direct
```

실제 코드 링크:
- [alembic/env.py:21](../../alembic/env.py#L21) — `settings.direct_url`(5432) 주입.
- [core/config.py:31](../../core/config.py#L31) — `direct_url` 프로퍼티(미설정이면 POOL 폴백).

### 3) 우리 소유 테이블만 건드리기 — `include_name`

같은 Supabase 프로젝트 안에는 우리 모델에 없는 테이블(예: Supabase 의 `auth.*`, 혹은 예전 `waitlist`)도 있습니다. autogenerate 가 "모델에 없네?" 하고 **남의 테이블을 DROP 해버리면 대참사**예요. `include_name` 이 "우리 모델에 정의된 테이블만" 관리하도록 필터링합니다.

```python
_OWNED_TABLES = set(Base.metadata.tables.keys())   # 우리가 정의한 테이블 집합
def include_name(name, type_, parent_names):
    if type_ == "table":
        return name in _OWNED_TABLES               # 우리 것만 True → 나머지는 무시
    return True
```

실제 코드 링크:
- [alembic/env.py:33](../../alembic/env.py#L33) — `_OWNED_TABLES` 집합.
- [alembic/env.py:36](../../alembic/env.py#L36) — `include_name()` 필터.
- [alembic/env.py:90](../../alembic/env.py#L90) — online 모드에서 `include_name` 연결(+ `compare_type`, `compare_server_default`).

### 흔한 함정

- **`env.py` 에서 `db.registry` 대신 특정 모델만 import 하면** 그 테이블만 인식돼 나머지가 autogenerate 에서 누락됩니다. 항상 레지스트리를 통해서 등록하세요.
- **마이그레이션에 6543(POOL)을 쓰면** pgbouncer 때문에 DDL 이 실패하거나 이상하게 동작할 수 있어요. 5432 direct 를 고집하는 데는 이유가 있습니다.

> 한 줄 요약: `env.py` 는 registry 로 14테이블을 인식하고, 5432 direct 로 붙고, `include_name` 으로 우리 테이블만 안전하게 관리합니다.

---

## ✍️ 스스로 점검

1. 모델 클래스에 컬럼을 추가하기만 하면 실제 DB 에 컬럼이 생기나요? 왜 그런가요/아닌가요?
2. 새 팀원이 빈 DB 로 프로젝트를 세팅할 때 `Base.metadata.create_all()` 을 쓰면 나중에 어떤 문제가 생기나요?
3. `alembic/env.py` 가 5432(direct)를 쓰고 `include_name` 필터를 두는 이유를 각각 한 문장으로 설명해 보세요.

⟵ [이전: 인증 — Supabase Auth 위임](03-auth-supabase.md) ・ [📚 목차](README.md) ・ [다음: 표준 수직 슬라이스 — account 도메인](05-account-vertical-slice.md) ⟶
