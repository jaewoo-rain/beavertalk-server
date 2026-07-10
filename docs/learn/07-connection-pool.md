# 07. Connection Pool — 매번 새로 연결하지 마라 (DB·HTTP)

> **한 줄 요약**: DB·HTTPS 연결은 열 때마다 **TCP 핸드셰이크 + TLS + 인증**이라는 왕복 비용을 낸다. **풀(pool)** 은 미리 만들어 둔 연결 몇 개를 재사용해 이 비용을 없앤다. 크기는 트레이드오프다 — 너무 작으면 대기(고갈), 너무 크면 DB 측 부담. 그리고 **워커수 × 풀사이즈 = 실제 DB 연결 수**(6장의 곱셈이 여기서 폭발한다).

**이 챕터의 키워드**: Connection Pool, Transaction, N+1 (Keep-Alive 는 15장 예고)

---

## 1. 왜 중요한가

우리 서버는 통화 1건마다 준비·저장·분석에서 DB 를 여러 번 두드리고, 목록·상세 화면은 매 요청 DB 를 친다. 만약 **매 쿼리마다 DB 에 새로 연결**한다면, 실제 SQL 실행(1ms)보다 **연결 수립(수 ms~수십 ms)** 이 훨씬 비싸 앱이 통째로 느려진다. 원격 Postgres 라면 TCP 3-way + TLS 핸드셰이크 + 비밀번호 인증까지 매번 왕복한다.

게다가 6장에서 배운 "워커를 늘리면 자원이 N배"가 **커넥션에서 가장 아프게** 나타난다. 워커 4개 × 풀 10이면 DB 연결이 40개다. Supabase·Postgres 의 `max_connections` 는 유한하고, 그걸 넘으면 신규 연결이 거절돼 앱이 죽는다. 이 장은 "연결은 비싸고 유한한 자원이며, 그래서 풀로 재사용하되 곱셈을 조심해야 한다"를 몸으로 익힌다.

## 2. 개념 — 비유로 시작

**비유**: 회의를 할 때마다 **건물을 새로 짓는** 회사를 상상해 보라. 회의는 10분인데 건물 짓는 데 3시간이 걸린다. 미친 짓이다. 대신 공유 오피스는 **회의실 몇 개를 미리 지어 두고**, 필요할 때 예약해 쓰고 끝나면 반납한다. 다음 사람은 그 방을 **그대로 재사용**한다.

- **회의실 = 연결(connection)**. 짓는 비용(핸드셰이크)은 처음 한 번만.
- **예약/반납 = 체크아웃(checkout)/반납(return)**. 풀에서 빌려 쓰고 돌려준다.
- **회의실 개수 = 풀 사이즈**. 3개뿐인데 4팀이 동시에 회의하려 하면 한 팀은 **줄 서서 대기**한다(= 풀 고갈).
- **반납 안 하면**: 회의 끝나고도 방을 안 비우면(커넥션 누수), 남은 방이 점점 줄어 결국 아무도 못 쓴다.

**정확한 정의**: 커넥션 풀은 물리적 DB 연결 N개를 **미리 열어(또는 필요 시 열어) 보관**하는 관리자다. 애플리케이션이 "연결 줘"(체크아웃)라고 하면 놀고 있는 연결을 빌려주고, 다 쓰면(반납) 닫지 않고 **풀에 되돌려** 다음 요청이 재사용한다. SQLAlchemy 의 기본 풀은 `QueuePool`이고, 반대로 `NullPool`은 "풀 안 씀 = 매번 새 연결/즉시 폐기"다.

> **Transaction 한 문단**: 연결과 짝을 이루는 개념이 **트랜잭션**이다. 하나의 연결 위에서 `BEGIN … COMMIT/ROLLBACK` 으로 묶인 작업 단위다. 우리 규율은 "**쓰기 후 service 에서 명시적 `db.commit()`**"(repository 는 커밋 안 함) — 이게 곧 트랜잭션 경계다. 트랜잭션을 **오래 열어 두면 그 연결을 그동안 붙잡고** 있어(반납이 늦어) 풀을 고갈시킨다. 특히 통화 WS 처럼 장수명 코루틴에서 세션을 오래 쥐면 치명적이라, 우리는 "짧게 열고 닫는" `run_db` 단위로만 DB 를 만진다(5절).

## 3. 그림

```
[풀 없음 — NullPool] 매 쿼리가 건물을 새로 짓는다
  요청1: (핸드셰이크 TCP+TLS+auth ~5ms)─[SELECT 1ms]─(닫기)
  요청2: (핸드셰이크 ~5ms)──────────────[SELECT 1ms]─(닫기)
  요청3: (핸드셰이크 ~5ms)──────────────[SELECT 1ms]─(닫기)
  => 100요청 = 핸드셰이크 100번 ≈ 500ms+

[풀 있음 — QueuePool] 미리 지은 방을 빌려 쓰고 반납
  준비:  (핸드셰이크 ~5ms) → 연결 A 를 풀에 보관
  요청1: [빌림]─[SELECT 1ms]─[반납]     <- 재사용, 핸드셰이크 0
  요청2: [빌림]─[SELECT 1ms]─[반납]     <- 재사용
  요청3: [빌림]─[SELECT 1ms]─[반납]     <- 재사용
  => 100요청 = 핸드셰이크 1번 ≈ 8ms

[곱셈 주의 — 6장] 워커 × 풀사이즈 = 실제 DB 연결
  워커1[풀 10] 워커2[풀 10] 워커3[풀 10] 워커4[풀 10] = DB 연결 40개
  DB 의 max_connections 를 넘으면 → 신규 연결 거절 = 장애
```

## 4. 직접 돌려보자

> ⚠️ 프로덕션 Supabase DB 에는 **일절 붙지 않는다.** 아래는 전부 로컬 SQLite 로, "연결 수립 비용"은 `connect` 이벤트에 5ms `sleep` 을 걸어 원격 DB 의 핸드셰이크를 흉내 냈다.

### (a) ⭐ 풀 없음(NullPool) vs 있음(QueuePool) — 실측

파일: [`examples/07_pool_vs_nullpool.py`](examples/07_pool_vs_nullpool.py)

```python
HANDSHAKE_S = 0.005   # 5ms: 원격 DB 연결 수립(TCP+TLS+auth) 비용 흉내
N = 100

@event.listens_for(eng, "connect")     # 물리 연결이 '새로 수립'될 때마다
def _simulate_handshake(dbapi_conn, rec):
    time.sleep(HANDSHAKE_S)            # ← 여기가 진짜 핸드셰이크 지점

for _ in range(N):
    with eng.connect() as conn:        # 체크아웃 → 쿼리 → 반납
        conn.execute(text("SELECT 1")).one()
```

실행:
```bash
uv run --with sqlalchemy python examples/07_pool_vs_nullpool.py
```

실제 출력:
```
작업: SELECT 1 을 100회. 연결 수립 비용 = 5ms 흉내

             NullPool (풀 없음) :   592.5 ms   (물리 connect 100회)
           QueuePool (풀 재사용) :     7.8 ms   (물리 connect 1회)

이론값: 풀 없으면 ~500ms(매번 핸드셰이크), 풀 있으면 처음 1회만 ~5ms
```

**어디를 보라**: `NullPool`은 **물리 connect 를 100회** 해서 핸드셰이크(5ms)를 100번 냈다 → 약 592ms. `QueuePool`은 첫 요청에 딱 **1번** 연결을 만들고 나머지 99번은 그 연결을 **재사용** → 7.8ms. **약 75배** 차이다. SQL 자체(`SELECT 1`)는 둘 다 똑같이 100번 돌았다 — 순수하게 **연결 수립 비용의 차이**만 본 것이다. 실제 원격 DB 는 핸드셰이크가 5ms 보다 훨씬 비쌀 때가 많아(TLS·네트워크 왕복) 이 격차는 더 벌어진다.

### (b) N+1 문제 — 쿼리 수를 실제로 세기

파일: [`examples/07_n_plus_1.py`](examples/07_n_plus_1.py)

부모(Call) 50개, 각자 자식(Sentence) 4개. 자식을 **lazy(기본)** 로 만지면 부모 1쿼리 + 자식 접근마다 1쿼리 = **1+N**. `selectinload` 는 부모 1쿼리 + 자식 IN 묶음 1쿼리 = **2**.

```python
# lazy: 부모 1쿼리 + c.sentences 접근 50번마다 각 1쿼리
calls = s.query(Call).all()
total = sum(len(c.sentences) for c in calls)     # ← 여기서 N번 추가 쿼리

# selectinload: 미리 IN 으로 한 번에
calls = s.query(Call).options(selectinload(Call.sentences)).all()
total = sum(len(c.sentences) for c in calls)     # 이미 로드됨 → 추가쿼리 0

# 쿼리 수는 before_cursor_execute 이벤트로 실제로 센다
```

실행:
```bash
uv run --with sqlalchemy python examples/07_n_plus_1.py
```

실제 출력:
```
부모(Call) 50개, 각자 자식(Sentence) 4개

  lazy (기본)      :  51 쿼리,   8.54 ms  (문장 200개)
  selectinload     :   2 쿼리,   3.34 ms  (문장 200개)

이론값: lazy = 1 + 50 = 51 쿼리 / selectin = 2 쿼리
```

**어디를 보라**: `lazy` 는 정확히 **51쿼리**(1 + 50). 부모 목록을 한 번 가져온 뒤, 각 부모의 `.sentences` 를 처음 만질 때마다 SQLAlchemy 가 몰래 `SELECT … WHERE call_id = ?` 를 하나씩 더 날린다. `selectinload` 는 **2쿼리** — 부모를 가져온 직후 그 50개 id 를 `WHERE call_id IN (…50개…)` 한 방으로 자식을 싹 긁어온다. 로컬 SQLite 라 시간 차(8.5ms vs 3.3ms)는 작지만, **실제 원격 DB 에선 쿼리 하나마다 왕복 지연(RTT)이 붙어** 50번 왕복 vs 1번 왕복의 차이가 수백 ms 로 벌어진다. N+1 은 "풀로 연결은 아꼈는데 쿼리 횟수로 다시 느려지는" 대표 함정이다.

### (c) 풀 고갈 — pool_size=2, 동시 3요청

파일: [`examples/07_pool_exhaustion.py`](examples/07_pool_exhaustion.py)

```python
eng = create_engine("sqlite:///:memory:", poolclass=QueuePool,
                    pool_size=2, max_overflow=0, pool_timeout=5)

def worker(i):
    with eng.connect() as conn:     # ← 빈 연결 없으면 여기서 대기
        conn.execute(text("SELECT 1")).one()
        time.sleep(0.5)             # 일하는 척 = 연결을 쥐고 있음

# 3 스레드 동시 투입 (연결은 2개뿐)
```

실행:
```bash
uv run --with sqlalchemy python examples/07_pool_exhaustion.py
```

실제 출력:
```
pool_size=2, max_overflow=0 → 동시에 최대 2연결. 3요청 동시 투입

  요청 1: 체크아웃  0.00s (대기 0.00s) → 0.5s 점유
  요청 2: 체크아웃  0.00s (대기 0.00s) → 0.5s 점유
  요청 3: 체크아웃  0.50s (대기 0.50s) → 0.5s 점유

관찰: 요청 1·2 는 대기 0. 요청 3 은 앞이 반납할 때까지 ~0.5s 대기.
```

**어디를 보라**: 연결이 2개(`pool_size=2`, `max_overflow=0` 로 초과 금지)뿐이라 요청 1·2 는 즉시 빌렸지만, 요청 3 은 **빌릴 연결이 없어 0.5초를 대기**했다 — 정확히 요청 1(또는 2)이 `time.sleep(0.5)` 를 끝내고 연결을 반납하는 시점까지. 만약 앞의 요청들이 `pool_timeout=5` 초를 넘겨 연결을 안 놓으면, 요청 3 은 **`TimeoutError`(풀 고갈)** 로 실패한다. 이게 "트랜잭션을 오래 잡으면 풀이 마른다"의 실체다.

## 5. 우리 코드와 연결

우리 스택은 특이하게도 **애플리케이션 풀을 끄고**(NullPool) pgbouncer 에 풀링을 위임하는 **이중 구조**다. 코드가 근거다.

- **`build_engine` 은 `NullPool`이다**: [`db/engine.py`](../../db/engine.py#L24) 는 `poolclass=NullPool` + `pool_pre_ping=True` 로 엔진을 만든다. 주석 그대로 "*Supabase Transaction Pooler(6543, pgbouncer) 뒤에서 동작하므로 SQLAlchemy 자체 풀은 끄고 pgbouncer 가 풀링을 담당*"한다. 왜냐면 **앱 풀(QueuePool) + pgbouncer 풀 이 이중으로 쌓이면** 연결 회계가 어긋나 고갈되기 때문. 즉 (a)의 "풀 있음" 역할을 SQLAlchemy 가 아니라 **pgbouncer 라는 서버측 풀**이 대신 한다 — 우리 앱 관점의 `NullPool`은 "로컬 풀 없음"이지 "연결 재사용 없음"이 아니다. `pool_pre_ping` 은 (c)류의 좀비 연결을 쿼리 전에 걸러 준다.
- **6543(POOL) vs 5432(DIRECT) — 왜 나뉘나**: [`core/config.py`](../../core/config.py#L23) 는 `DATABASE_URL_POOL`(런타임, 6543 pgbouncer **transaction pooler**)과 `DATABASE_URL_DIRECT`(마이그레이션, 5432 **직결**)를 분리한다. **런타임은 6543** — pgbouncer 가 트랜잭션 단위로 소수의 서버 연결을 수천 클라이언트가 나눠 쓰게 해 커넥션을 아낀다. **마이그레이션은 5432 직결** — transaction 모드 풀러에서는 `CREATE TABLE`/`ALTER` 같은 DDL, prepared statement, 세션 상태(`SET`)가 **트랜잭션 경계를 넘어 유지되지 않아** Alembic 이 깨진다. 그래서 스키마 변경은 반드시 직결로 한다(CLAUDE.md 의 Alembic 규율과 같은 이유).
- **세션 팩토리와 트랜잭션 경계**: [`db/session.py`](../../db/session.py#L22) 의 `build_session_factory` 는 `autocommit=False, autoflush=False, expire_on_commit=False`. `expire_on_commit=False` 라서 **커밋 후에도 ORM 객체 속성에 접근**할 수 있다(커밋 직후 응답 직렬화 때 재쿼리가 안 터진다 — N+1 예방과 결이 같다). [`get_db`](../../db/session.py#L32) 는 세션 생성/`close` 만 하고 **커밋은 안 한다** — 쓰기의 커밋은 service 몫(트랜잭션 경계).
- **N+1 을 구조적으로 회피하는 실제 코드**: [`alarm_repository.py`](../../domains/alarm/repository/alarm_repository.py#L21) 는 `[selectinload(Alarm.schedules), joinedload(Alarm.character)]` 로 "**컬렉션은 selectin, 스칼라는 joined**"라는 정석을 쓴다. [`call_repository.py`](../../domains/learning/repository/call_repository.py#L27) 의 상세 조회도 `joinedload(Call.character)` + `selectinload(Call.sentences).joinedload(Sentence.evaluation)` 로 중첩까지 미리 로드해, (b)의 51쿼리를 **몇 쿼리로** 눌렀다. [`dispatch_service.py`](../../domains/push/service/dispatch_service.py#L53) 의 예약전화 발송도 알람 N개를 돌기 전에 `selectinload(Alarm.schedules)` 로 자식을 한 번에 당겨온다 — 루프 안에서 lazy 접근했다면 알람 수만큼 쿼리가 터졌을 것.
- **장수명 WS 에서 연결을 오래 쥐지 않기**: [`normalcall_service.py`](../../domains/learning/service/normalcall_service.py#L50) 의 `run_db` 는 매번 **새 세션을 열어 fn 실행 후 즉시 close** 한다(주석: "*장수명 WS 가 세션을 오래 점유하지 않도록 짧게 열고 닫는 단위로만*"). 통화는 5분간 이어지지만 그동안 DB 연결을 붙잡고 있으면 (c)처럼 풀(=pgbouncer 슬롯)이 마른다. 그래서 "필요할 때만 짧게 빌리고 곧장 반납"한다 — 트랜잭션을 짧게 유지하는 이 규율이 6543 풀러에서 특히 중요하다.

## 6. 흔한 오해 / 함정

- ❌ **"풀을 크게 잡으면 무조건 빠르다."** → DB 의 `max_connections` 와 pgbouncer 한계가 상한이다. 앱 풀이 커도 DB 가 못 받으면 그 위에서 대기·거절이 난다. 풀 크기는 "DB 가 감당할 수 있는 총량 ÷ 워커 수"로 역산해야 한다.
- ❌ **"워커랑 풀은 별개다."** → **워커수 × 풀사이즈 = 실제 DB 연결 수**(6장). 워커 4 × 풀 10 = 40. 워커를 늘리면 커넥션이 **곱으로** 는다. 우리가 컨테이너당 워커 1 + NullPool + pgbouncer 로 가는 이유가 이 곱셈을 피하려는 것.
- ⚠️ **커넥션 반납을 잊으면 누수 → 고갈**: (c)에서 봤듯 안 돌려준 연결은 다음 요청을 굶긴다. 우리는 `get_db` 의 `finally: db.close()` 와 `run_db` 의 `finally: db.close()` 로 반납을 보장한다. `with` 없이 `eng.connect()` 를 직접 열고 안 닫으면 새는 지름길.
- ⚠️ **트랜잭션 오래 잡기 = 6543 풀러에서 치명**: transaction 모드 pgbouncer 는 **트랜잭션 단위로** 서버 연결을 나눠준다. 긴 트랜잭션(느린 외부호출을 트랜잭션 안에서 대기 등)은 그 슬롯을 오래 점유해 전체 처리량을 떨어뜨린다. 커밋을 빨리, 외부 I/O 는 트랜잭션 밖에서.
- ⚠️ **transaction 모드에서 prepared statement / 세션 상태 주의**: 같은 세션에서 `PREPARE` 한 statement 나 `SET`/temp table 이 다음 트랜잭션에선 다른 서버 연결로 가 사라질 수 있다. psycopg2 는 대체로 무난하지만, prepared statement 를 캐시하는 드라이버(asyncpg 등)는 6543 에서 문제를 낸다 — 그래서 DDL/마이그레이션은 5432 직결로 못 박았다.
- ❌ **"N+1 은 풀이랑 무관하니 이 장 밖 얘기다."** → 아니다. 풀로 연결 비용을 없애도 **쿼리 횟수**가 많으면(원격 RTT × N) 도로 느려진다. 연결 재사용(a)과 쿼리 수 줄이기(b)는 한 세트다.

## 7. 요약

- DB·HTTPS 연결은 **핸드셰이크(TCP+TLS+auth)** 비용이 커서, 매번 새로 열면 SQL 보다 연결이 더 비싸다. **풀**이 연결을 재사용해 이 비용을 없앤다((a): 592ms → 7.8ms).
- **풀 크기 트레이드오프**: 작으면 대기·고갈((c)), 크면 DB·메모리 부담. **워커수 × 풀사이즈 = 실제 DB 연결**(6장의 곱셈).
- **N+1**: lazy 접근은 1+N 쿼리((b): 51개). `selectinload`(컬렉션)·`joinedload`(스칼라)로 1~2쿼리로 줄인다. 우리 repository 들의 실제 패턴.
- **Transaction = 연결 점유 단위**. service 에서 **짧게 열고 명시적 커밋**. 오래 잡으면 6543 풀러를 고갈시킨다.
- **우리 구조**: 앱은 `NullPool`(로컬 풀 OFF) + `pool_pre_ping`, 풀링은 **pgbouncer(6543)** 가 담당. **마이그레이션만 5432 직결**(transaction 풀러의 DDL/prepared statement 이슈 회피).

## 8. 연습문제

1. `07_pool_vs_nullpool.py` 의 `HANDSHAKE_S` 를 0.005 → 0.02(20ms)로 올리면 NullPool/QueuePool 시간은 각각 대략 어떻게 될까? 두 값의 **비율**은?
2. `07_n_plus_1.py` 에서 `CHILDREN_EACH` 를 4 → 20 으로 늘리면 `lazy` 와 `selectinload` 의 **쿼리 수**는 각각 어떻게 변할까? (부모는 50 그대로)
3. 우리 서버를 컨테이너당 워커 1 이 아니라 `--workers 4` 로 바꾸고 각 워커가 QueuePool(pool_size=10)을 쓴다면, DB 연결은 최대 몇 개가 될까? 그게 왜 위험한가? 우리가 실제로 택한 구조(`NullPool` + pgbouncer)는 이걸 어떻게 피하나?

<details>
<summary>답</summary>

1. NullPool 은 핸드셰이크 100번이라 **약 2000ms(2초)** 로 4배 증가(20ms × 100). QueuePool 은 첫 1번만 내므로 **약 20ms + 쿼리시간 ≈ 20여 ms** 로 거의 그대로. 비율은 5ms 때 ~75배에서 **더 벌어진다**(수백 배). 연결 수립이 비쌀수록 풀의 이득이 커진다는 뜻.
2. **쿼리 수는 둘 다 안 변한다.** `lazy` 는 여전히 **51쿼리**(1 + 부모 50; 자식이 몇 개든 부모당 1쿼리), `selectinload` 도 **2쿼리**. N+1 의 N 은 **부모 수**지 자식 수가 아니다. 다만 각 쿼리가 실어 나르는 행(row) 수는 늘어 데이터 전송량은 커진다.
3. 최대 **4 × 10 = 40 커넥션**(+ 각 워커의 max_overflow 만큼 더). Supabase/Postgres 의 `max_connections` 는 유한해서, 인스턴스가 오토스케일로 늘면 40 × 인스턴스수로 폭증해 **연결 거절 → 장애**가 난다. 우리는 앱 풀을 `NullPool` 로 꺼서 워커가 연결을 쌓아두지 않게 하고, 풀링을 **pgbouncer(6543)** 한 곳에 모아 소수의 서버 연결을 트랜잭션 단위로 공유한다 — 워커·인스턴스가 늘어도 DB 쪽 실제 연결 수가 곱으로 터지지 않는다.

</details>

---

다음 챕터 → [08. Profiling — 추측하지 말고 측정하라](08-profiling.md)

돌아가기 → [교과서 목차(README)](README.md)
