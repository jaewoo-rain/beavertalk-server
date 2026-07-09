# 14. Redis Cache — 안 하는 게 제일 빠른 계산

> **한 줄 요약**: 가장 빠른 계산은 **아예 하지 않는 계산**이다. 자주 읽고 잘 안 변하는 데이터는 **캐시(메모리)** 에 둬 DB 왕복·외부호출·무거운 조립을 건너뛴다. **Redis** 는 별도 프로세스로 도는 초고속 인메모리 key-value 저장소다. 핵심은 패턴(Cache-Aside)·유효기간(TTL)·방출정책(LRU), 그리고 **낡은 데이터(stale)를 어떻게 막느냐**다.

**이 챕터의 키워드**: Redis, Cache-Aside, Write-Through, Write-Back, TTL, LRU, Cache Hit/Miss, Hit Ratio

---

## 1. 왜 중요한가

7장에서 "연결은 비싸니 풀로 재사용한다"를 배웠다. 이 장은 한 걸음 더 간다 — **연결도, 쿼리도, 아예 안 하면** 그게 제일 빠르다. 우리 서버는 통화 1건을 시작할 때마다 [`load_call_setup`](../../domains/learning/service/normalcall_service.py#L69) 이 회원·레벨·캐릭터·음색을 DB 에서 긁어와 프롬프트를 조립한다. 그런데 **레벨(12행)·캐릭터·음색은 거의 안 변하는 마스터 데이터**다. 통화 100건이면 똑같은 `Level(1)` 을 100번 DB 에서 읽는다. 매번 pgbouncer 슬롯을 빌리고, 쿼리를 날리고, 원격 RTT 를 문다.

캐시는 이걸 **첫 1번만 DB, 이후는 메모리**로 바꾼다. 8장에서 본 P99 지연의 상당 부분이 DB 왕복인데, **캐시 히트는 그 왕복을 0 으로** 만들어 꼬리 지연을 직접 깎는다. 대신 공짜가 아니다 — 캐시는 원본의 **복사본**이라, 원본이 바뀌면 캐시가 거짓말을 하기 시작한다("stale"). "캐시 무효화와 이름 짓기는 CS 의 2대 난제"라는 농담이 괜히 있는 게 아니다. 이 장은 캐시로 **얼마나 빨라지는지**와, 그 대가인 **낡음을 어떻게 통제하는지**를 같이 몸으로 익힌다.

> 참고: 우리는 **아직 Redis 를 안 쓴다**(현재 코드에 캐시 계층 없음). 이 장은 "어디에 캐시를 넣으면 이득인가"를 실제 코드로 짚는 설계 장이다. 예제는 Redis 서버가 이 머신에 없어 **fakeredis**(순수 파이썬 인메모리, redis-py 와 API 동일)로 돌렸다. 코드는 실서버와 똑같고, `fakeredis.FakeStrictRedis()` 를 `redis.Redis(host=...)` 로 **한 줄만 바꾸면** 실 Redis 에 붙는다.

## 2. 개념 — 비유로 시작

**비유**: 매번 도서관(DB)까지 걸어가지 마라. 자주 보는 책 몇 권은 **책상 위(캐시)** 에 둔다. 필요할 때 손만 뻗으면 되니(메모리 접근) 도서관 왕복(디스크+네트워크)이 사라진다. 단, 책상은 좁아서(메모리 유한) **무한히 못 쌓는다** — 꽉 차면 안 보는 책부터 도서관에 돌려보내야 한다(LRU 방출). 그리고 도서관 원본이 개정판으로 바뀌면 책상 위 구판은 **틀린 정보**가 된다 — 그래서 "이 책은 2주 뒤 반납"(TTL) 같은 유효기간을 둔다.

- **책상 = 캐시(Redis, 메모리)**. 도서관(DB, 디스크/원격)보다 수백~수천 배 빠르다.
- **책이 책상에 있음 = Cache Hit**. 없어서 도서관 감 = **Cache Miss**.
- **hit ratio = 손 뻗어 해결한 비율**. 이게 높을수록 캐시가 일을 한 것.
- **책상 꽉 참 → 안 보는 책 반납 = LRU eviction**(Least Recently Used).
- **2주 뒤 반납 = TTL**. 유효기간이 지나면 알아서 사라져 다음엔 최신판을 다시 가져온다.

**정확한 정의**: Redis(REmote DIctionary Server)는 데이터를 **디스크가 아니라 RAM 에** 두는 key-value 저장소다. 별도 프로세스(보통 별도 서버)로 돌고, 앱은 TCP 로 명령을 보낸다(`GET`, `SET`, `SETEX`, `EXPIRE`, `TTL`, `DEL` …). 인메모리라 읽기/쓰기가 마이크로초 단위다. 캐시는 이 Redis 를 "원본(DB) 앞에 둔 빠른 사본 계층"으로 쓰는 것이다.

> **Cache-Aside 한 문단**: 가장 흔한 패턴. 앱이 캐시를 **옆에 두고(aside) 직접 관리**한다. 읽기는 ①캐시 확인 → ②hit 면 즉시 반환(DB 안 침) → ③miss 면 원본 조회 후 캐시에 채움. 캐시는 원본을 **모르고**, 앱이 둘을 오간다. 반대로 Write-Through/Write-Back 은 "쓸 때" 전략인데 4절 (d)에서 실측한다.

## 3. 그림

```
[캐시 없음] 매 조회가 도서관까지 왕복
  통화1 시작: load_call_setup → (DB 왕복 ~수십 ms) Level·Character·Voice
  통화2 시작: load_call_setup → (DB 왕복 ~수십 ms) 똑같은 Level·Character·Voice
  통화3 시작: load_call_setup → (DB 왕복 ~수십 ms) 또 똑같은 것
  => 안 변하는 데이터를 매번 다시 읽음. pgbouncer 슬롯·RTT 낭비

[Cache-Aside] 첫 1번만 DB, 이후는 책상 위에서
  통화1: GET level:1 → (miss) DB 조회 ~수십 ms → SET level:1
  통화2: GET level:1 → (HIT) ~0.1ms, DB 안 감  ← 왕복 0
  통화3: GET level:1 → (HIT) ~0.1ms, DB 안 감
  => miss 는 최초 1번. hit ratio ↑ → P99 ↓ (8장)

[낡음 통제] 원본이 바뀌면 사본은 거짓말
  level 데이터 수정  →  캐시엔 여전히 구판(stale)
  방어1: TTL — N초 뒤 자동 소멸 → 다음 읽기가 최신 재적재
  방어2: 쓰기 때 캐시 DEL(무효화) → 다음 읽기가 재적재

[꽉 차면] 책상은 유한 → 방출
  maxmemory 초과 → allkeys-lru → 가장 오래 안 쓴 키부터 버림
```

## 4. 직접 돌려보자

> ⚠️ 원격 Redis·DB 에는 **일절 붙지 않는다.** 캐시는 **fakeredis**(프로세스 메모리)로, '느린 원본(DB)'은 `time.sleep` 으로 흉내 냈다. 실행 환경은 Windows / Python 3.14 / `uv run --with fakeredis`. 실서버는 각 파일의 `build_redis()` 주석대로 `redis.Redis(host=...)` 로 바꾸면 코드는 그대로다.

### (a) ⭐ Cache-Aside — miss(느림) vs hit(빠름) + hit ratio

파일: [`examples/14_cache_aside.py`](examples/14_cache_aside.py)

```python
def get_character(r, character_id: int) -> str:
    key = f"character:{character_id}"
    cached = r.get(key)          # 1) 캐시 확인
    if cached is not None:       # 2) hit → 즉시 반환 (DB 안 침)
        stats["hit"] += 1
        return cached.decode()
    stats["miss"] += 1           # 3) miss → 느린 원본 조회
    value = load_character_from_db(character_id)   # time.sleep(0.3) 흉내
    r.set(key, value)            #    캐시에 채워 둔다(다음엔 hit)
    return value
```

실행:
```bash
uv run --with fakeredis python examples/14_cache_aside.py
```

실제 출력:
```
작업: character:7 조회. 원본(DB) 지연 = 300ms

  1번째(miss, DB 감) :   300.61 ms
  2번째(hit, 캐시)   :     0.09 ms
  → 3,420배 빠름

  총 조회 1002회 → hit 1001, miss 1
  hit ratio = 99.9%  (miss 는 최초 1번뿐, 나머지는 전부 캐시)
```

**어디를 보라**: 첫 호출은 캐시가 비어 있어 **miss** → 느린 원본(300ms)을 치고 결과를 `SET`. 두 번째부터는 같은 키가 캐시에 있어 **hit** → **0.09ms**, DB 를 아예 안 갔다. **약 3,400배**다. 1002번 조회 중 **miss 는 최초 1번뿐**이고 나머지 1001번은 전부 캐시가 해결해 **hit ratio 99.9%**. 이게 캐시의 본질이다 — "자주 읽고 잘 안 변하는" 데이터일수록 miss 는 한 줌이고 hit 가 압도한다. 우리 `load_call_setup` 의 레벨·캐릭터·음색이 정확히 이 성질이다.

### (b) TTL — 유효기간을 줘 낡음을 자동 소멸

파일: [`examples/14_ttl.py`](examples/14_ttl.py)

```python
r.set("level:1", "Level(1, 초급)", ex=2)   # 값 + 2초 TTL (setex 와 동일)
# r.ttl(key): 남은 수명(초). -2=키 없음, -1=TTL 없음(영구)
```

실행:
```bash
uv run --with fakeredis python examples/14_ttl.py
```

실제 출력:
```
작업: set(ex=2) 로 TTL 2초 부여 후 시간에 따른 상태 관찰

        0.0s → HIT   ttl= 2s  value=Level(1, 초급)
        1.0s → HIT   ttl= 1s  value=Level(1, 초급)
        2.2s → MISS  ttl=-2s  value=(사라짐)

  → 만료 후엔 MISS. Cache-Aside 라면 여기서 다시 DB 를 쳐 최신값으로 재적재한다.
  (ttl=-2 는 '키 없음' = 완전히 증발했다는 뜻)
```

**어디를 보라**: `set(..., ex=2)` 로 넣은 키는 남은 수명(`ttl`)이 2 → 1 로 줄다가, 2초를 넘긴 2.2s 시점엔 **MISS**(`ttl=-2` = 키 자체가 없음). TTL 이 하는 일은 단순하지만 강력하다 — **"낡음의 최대치를 시간으로 못박는다"**. 레벨 데이터를 캐시하되 `ex=600`(10분) 을 주면, 운영자가 레벨 프로파일을 고쳐도 **최대 10분 안에** 캐시가 스스로 만료돼 최신값을 다시 물어온다. 무효화 로직을 안 짜도 "일정 시간 뒤엔 반드시 최신"이 보장된다. 값이 자주 바뀔수록 TTL 을 짧게, 거의 안 바뀔수록 길게.

### (c) LRU — 캐시가 꽉 차면 가장 오래 안 쓴 걸 버린다

파일: [`examples/14_lru.py`](examples/14_lru.py)

Redis 에선 서버 설정으로 켠다:
```
maxmemory 256mb
maxmemory-policy allkeys-lru   # 메모리 초과 시 전체 키 중 LRU 를 방출
# volatile-lru = TTL 있는 키 중에서만 / allkeys-lfu = 접근'빈도' 기반 등도 있음
```
fakeredis 로는 maxmemory eviction 재현이 제한적이라, **개념 자체**는 파이썬 표준 `functools.lru_cache` 로 정직하게 실측한다(동작 원리는 allkeys-lru 와 같다: 꽉 차면 LRU 를 버린다).

```python
@lru_cache(maxsize=3)          # 최대 3개만 기억. 초과하면 LRU 를 버린다.
def compute(x): ...
# 1,2,3 채움 → 1 재조회(최신화) → 4 삽입(자리없음→가장오래된 2 방출)
```

실행:
```bash
uv run python examples/14_lru.py
```

실제 출력:
```
작업: lru_cache(maxsize=3) 에 값을 넣고, 넘칠 때 무엇이 방출되는지 관찰

  1,2,3 계산      → cache_info: CacheInfo(hits=0, misses=3, maxsize=3, currsize=3)
  1 을 다시 조회   → hit. LRU 순서: 2(오래됨) < 3 < 1(최신)
  4 삽입          → 꽉 참. 가장 오래 안 쓴 '2' 를 방출

  2 를 다시 조회   → 재계산됨(miss)  ← 방출됐다는 증거
  1 을 다시 조회   → 캐시에 남아있음(hit)  ← 자주 쓴 건 살아남음

  최종 cache_info : CacheInfo(hits=2, misses=5, maxsize=3, currsize=3)
  '진짜 계산' 실행된 순서 : [1, 2, 3, 4, 2]
  → 1,2,3,4 채운 뒤 2 만 다시 계산됨. 방출된 건 딱 '가장 오래 안 쓴 2'.
```

**어디를 보라**: `maxsize=3` 에 1·2·3 을 채운 뒤 **1 을 다시 조회**하면 1 이 "최신 사용"으로 올라가고 **2 가 가장 오래된 것**이 된다. 이 상태에서 4 를 넣으면 자리가 없어 **가장 오래 안 쓴 2 를 방출**한다. 증거는 마지막 줄 — '진짜 계산'이 실행된 순서가 `[1, 2, 3, 4, 2]` 로, **2 만 다시 계산**됐다(방출됐으니 miss). 반면 자주 건드린 1 은 살아남아 hit. 이것이 LRU 의 정신이다: **"인기 있는 건 남기고, 잊힌 건 버린다."** Redis 의 `allkeys-lru` 도 키 규모만 클 뿐 판단 기준은 똑같다. TTL 을 안 걸어도 maxmemory + LRU 가 있으면 캐시가 무한정 부풀지 않는다.

### (d) 쓰기 전략 — Write-Through vs Write-Back vs Cache-Aside(무효화)

파일: [`examples/14_write_strategies.py`](examples/14_write_strategies.py)

읽기는 Cache-Aside 하나로 대개 충분하지만, **쓸 때** 캐시를 어떻게 다루느냐가 갈린다.

```python
def write_through(r, key, value):
    r.set(key, value)            # 캐시 갱신
    db_write(key, value)         # + DB 도 즉시 (둘 다 끝나야 반환) → 항상 일관, 느림

def write_back(r, key, value):
    r.set(key, value)            # 캐시만 갱신하고
    dirty[key] = value           # DB 로 내릴 목록에 적어두고 즉시 반환(느린 DB 안 기다림)
# flush_dirty(): 나중에 몰아서 DB 반영 → 그 전에 크래시면 유실
```

실행:
```bash
uv run --with fakeredis python examples/14_write_strategies.py
```

실제 출력:
```
작업: 같은 키에 10번 쓰기. DB write 지연 = 50ms

  Write-Through :    505.3 ms  (매 쓰기가 DB 대기, 항상 일관)
                  캐시=Beaver-9  DB=Beaver-9  ← 일치

  Write-Back    :      0.4 ms  (DB 안 기다림 → 폭발적으로 빠름)
                  flush 전: 캐시=Beaver-9  DB=None  ← DB 아직 낡음/없음
                  flush(1건) 후: DB=Beaver-9  ← 이제 반영. 하지만 flush 전 크래시였다면 유실

  → Write-Back 가 Write-Through 보다 약 1,124배 빠른 쓰기. 대가는 유실 위험.
```

**어디를 보라**: **Write-Through** 는 매 쓰기가 DB(50ms)를 기다려 10번에 505ms — 대신 캐시와 DB 가 **항상 일치**한다. **Write-Back** 은 캐시만 찍고 즉시 반환해 10번에 **0.4ms**(1,000배+). 게다가 같은 키 10번 쓰기가 flush 땐 **1건으로 합쳐졌다**(write coalescing) — DB 쓰기 횟수 자체를 줄인다. 대가는 명확하다: flush **전에 프로세스가 죽으면 그 사이 쓰기는 증발**한다(출력의 `DB=None` 구간). 정리하면 아래 표.

| 전략 | 쓸 때 하는 일 | 쓰기 속도 | 일관성 | 유실 위험 | 언제 쓰나 |
|---|---|---|---|---|---|
| **Cache-Aside(무효화)** | DB 에 쓰고 캐시는 **DEL**(다음 읽기가 재적재) | 보통(DB 1회) | 강함(다음 읽기부터 최신) | 없음 | **대부분의 읽기 위주** 데이터. 우리 기본값 |
| **Write-Through** | 캐시 + DB **동시** 갱신 | 느림(DB 대기) | **가장 강함**(항상 일치) | 없음 | 읽자마자 캐시 hit 이 중요하고 정합성이 최우선 |
| **Write-Back** | 캐시만 갱신, DB 는 **나중에 배치** | **가장 빠름** | 약함(잠시 불일치) | **있음**(flush 전 크래시) | 쓰기 폭주 + 약간의 유실 감내 가능(카운터·조회수 등) |

실무 기본은 **읽기=Cache-Aside, 쓰기=DB 갱신 후 캐시 DEL(무효화)** 조합이다. Write-Back 은 빠르지만 유실 위험 때문에 "정확성이 덜 중요한 고빈도 쓰기"(예: 조회수 카운터)에만 신중히 쓴다.

## 5. 우리 코드와 연결 (정직히)

우리는 **아직 Redis 캐시가 없다.** 아래는 "넣는다면 어디가 이득인가"를 실제 코드로 짚은 설계 제안이다.

- **캐시하면 이득 — 마스터 데이터**: [`load_call_setup`](../../domains/learning/service/normalcall_service.py#L69) 은 통화 시작마다 `Level`·`Character`·`Voice` 를 DB 에서 읽는다. [`level.py`](../../domains/learning/models/level.py) 는 주석부터 "마스터 데이터(12단계)"이고 [`character.py`](../../domains/commerce/models/character.py) 의 role·personality·voice 도 운영 중 거의 안 바뀐다. 이건 (a)의 `character:7` 과 판박이 — **첫 통화만 miss, 이후 전부 hit** 이 되는 이상적 캐시 대상이다. `character:{id}`·`level:{no}` 키에 **TTL 10분**((b) 방식)이면 무효화 로직 없이도 "최대 10분 낡음"으로 통제된다. 캐릭터/레벨을 운영자가 수정하는 경로에서 해당 키를 **DEL(무효화)** 해 주면 (d)의 Cache-Aside 쓰기가 완성된다.
- **캐시 부적합 — 개인화·실시간**: 회원의 통화 이력(`_load_history`)·현재 통화 상태·알람 도래 시각 같은 **개인화/실시간** 값은 자주 바뀌고 사용자마다 달라 hit ratio 가 낮고 stale 위험이 크다. 캐시하려면 **아주 짧은 TTL**(수 초)이나 아예 캐시하지 않는 게 낫다. 특히 통화 진행 상태는 실시간 정확성이 생명이라((d) Write-Back 부적합) 캐시에 두지 않는다.
- **7장·8장과의 연결**: 캐시 히트는 **DB 왕복 자체를 없애** pgbouncer(6543) 슬롯 점유와 원격 RTT 를 0 으로 만든다. 7장의 "연결은 유한하니 짧게"에서 한 발 더 나아가 **연결을 아예 안 여는** 것이고, 8장의 P99 관점에선 **꼬리 지연의 주범인 DB 왕복을 제거**해 P99 를 직접 낮춘다. 단, Redis 도 **원격이면 네트워크 홉**이라는 점을 잊지 마라 — 로컬 프로세스 캐시(`lru_cache`)는 홉이 0 이지만 워커·인스턴스마다 따로 놀고, 원격 Redis 는 모든 워커가 공유하지만 홉이 하나 붙는다(6절 트레이드오프).
- **미래 — 캐시 밖의 Redis(부록 예고)**: Redis 는 캐시 외에도 **rate-limit**(요청 카운터 + TTL), **분산 락**(여러 워커가 같은 통화 분석을 중복 실행 못 하게), **세션 저장소**, **큐/스트림**(통화후 분석 잡 큐잉)으로도 쓴다. 우리 스택에 도입한다면 캐시가 첫 단추, 분산 락·큐는 그다음 후보다.

## 6. 흔한 오해 / 함정

- ⚠️ **캐시 무효화(stale data) — CS 2대 난제**: 원본이 바뀌었는데 캐시가 구값을 계속 hit 시키면, **연산 자체는 빠른데 답이 틀린** 최악이 된다. 방어는 셋 — ①**TTL**((b), "낡음의 상한을 시간으로"), ②**쓰기 시 DEL/갱신**((d) Cache-Aside/Write-Through), ③둘 다. "캐시했더니 옛날 값이 안 바뀌어요"의 99%가 무효화 누락이다.
- ⚠️ **TTL 없이 영구 캐시**: `SET` 만 하고 `ex` 를 안 주면 그 키는 **영원히** 산다. 메모리는 무한정 부풀고((c)의 maxmemory 없으면 OOM), 원본이 바뀌어도 영영 낡은 채다. **캐시엔 원칙적으로 TTL 을 걸어라**(또는 maxmemory+LRU 로 상한). "잠깐만 넣자"가 제일 오래 산다.
- ⚠️ **캐시 스탬피드(stampede)**: 인기 키의 TTL 이 만료되는 **바로 그 순간**, 그 키를 기다리던 수백 요청이 동시에 miss → **전부 원본으로 몰려** DB 를 폭격한다(캐시가 방패를 내린 찰나 원본이 맞는다). 완화책: 만료 임박 시 **한 요청만 재계산하게 락**(single-flight)·재계산 중엔 구값 잠깐 서빙(stale-while-revalidate)·TTL 에 **지터(랜덤 오프셋)** 를 줘 만료를 흩뿌리기.
- ❌ **"뭐든 캐시하면 좋다"**: 민감정보(토큰·개인정보)를 공유 Redis 에 함부로 두면 보안 리스크, 거대 값(대용량 오디오·바이너리)을 캐시하면 메모리·네트워크 전송이 되레 비싸다. **작고, 자주 읽고, 잘 안 변하고, 민감하지 않은** 것만 캐시하라.
- ⚠️ **직렬화 비용**: Redis 는 문자열/바이트만 저장한다. dict·객체를 넣으려면 `json.dumps`(직렬화), 꺼낼 때 `json.loads`(역직렬화)가 든다 — 이 비용이 크면 캐시 이득을 갉아먹는다. 무엇으로 직렬화하느냐(json vs orjson vs Protobuf)가 성능을 바꾸는데, 이는 **직렬화(부록 예정)** 에서 따로 다룬다.
- ⚠️ **Redis 도 네트워크 홉이다**: 원격 Redis 는 프로세스 밖이라 **TCP 왕복 1회**가 붙는다(로컬 `lru_cache` 는 0). "메모리니까 공짜"가 아니다 — DB(수십 ms)보다야 훨씬 싸지만, **초고빈도·초저지연**이 필요하면 로컬 인프로세스 캐시 + 원격 Redis 를 **2단(L1/L2)** 으로 겹치기도 한다. 로컬은 빠르지만 워커별로 따로 놀고 무효화가 어렵다는 트레이드오프.

## 7. 요약

- 가장 빠른 계산은 **안 하는 계산**. 자주 읽고 잘 안 변하는 데이터는 **캐시(메모리)** 에 둬 DB 왕복·외부호출을 건너뛴다. **Redis** = 별도 프로세스로 도는 초고속 인메모리 key-value.
- **Cache-Aside**((a)): 읽기 → 캐시 확인 → hit 즉시 반환 / miss 면 원본 조회 후 `SET`. miss 는 최초 한 줌, 이후 **hit ratio 99%+**. (a) 실측 miss 300ms vs hit 0.09ms.
- **TTL**((b)): `set(..., ex=N)` 로 유효기간 → 만료 시 자동 소멸 → 다음 읽기가 최신 재적재. **낡음의 상한을 시간으로 못박는** 가장 단순한 무효화.
- **LRU**((c)): 캐시가 꽉 차면 **가장 오래 안 쓴 것부터 방출**(`maxmemory-policy allkeys-lru`). 인기 있는 건 남고 잊힌 건 버려진다.
- **쓰기 전략**((d)): Cache-Aside(무효화)=기본 / Write-Through=항상 일관·느림 / Write-Back=폭발적 빠름·유실 위험. Write-Back 은 1,124배 빨랐지만 flush 전 크래시 = 유실.
- **우리 적용**: `Level`·`Character`·`Voice` 마스터 데이터가 이상적 캐시 대상(`load_call_setup`), 개인화·실시간은 부적합. 캐시 히트는 **DB 왕복을 없애 P99 를 낮춘다**(8장). 함정은 **무효화·TTL 누락·스탬피드·직렬화 비용·네트워크 홉**.

## 8. 연습문제

1. `14_cache_aside.py` 에서 `DB_LATENCY_S` 를 0.30 → 0.05(50ms)로 낮추면 "1번째(miss)"와 "2번째(hit)" 시간, 그리고 배수는 어떻게 바뀔까? hit ratio 는?
2. `14_ttl.py` 의 `TTL_S` 를 2 → 5 로 올리면 `2.2s` 시점의 상태는 HIT 일까 MISS 일까? 왜? 실제 서비스에서 "레벨 데이터"와 "환율 시세"에 각각 어떤 TTL 을 주는 게 합리적일지 이유와 함께.
3. `load_call_setup` 이 캐시하는 `character:{id}` 를 운영자가 캐릭터 정보를 수정했다. TTL 만으로 통제할 때 최악의 낡음은 몇 분인가? 그 낡음을 0 에 가깝게 하려면 (d)의 어떤 전략/동작을 추가해야 하나?

<details>
<summary>답</summary>

1. 1번째는 약 **50ms**(원본 지연 그대로), 2번째는 여전히 **~0.1ms**(캐시는 원본 지연과 무관). 배수는 300ms 때 ~3,400배에서 **~500배 정도로 줄어든다** — 원본이 쌀수록 캐시 이득의 절대 배수는 작아진다(그래도 여전히 큼). **hit ratio 는 그대로 99.9%** — 지연은 hit/miss 비율과 무관하고, miss 는 여전히 최초 1번뿐이기 때문.
2. **HIT**. TTL 이 5초라 2.2초 시점엔 아직 3초 가까이 남아 만료 전이다. 레벨 데이터는 거의 안 변하니 **긴 TTL**(수 분~수십 분)이 합리적 — 낡아도 큰 문제 없고 hit ratio 를 높인다. 환율 시세는 초 단위로 바뀌고 낡으면 손해가 크니 **아주 짧은 TTL**(수 초) 또는 실시간 갱신. TTL 은 "그 데이터가 얼마나 낡아도 되는가"로 정한다.
3. TTL 이 10분이면 최악 **약 10분** 낡을 수 있다(수정 직후 만료 직전 값을 hit 하면). 0 에 가깝게 하려면 **수정 경로에서 해당 키를 즉시 `DEL`(무효화)** 하거나 **Write-Through 로 캐시+DB 동시 갱신** — 그러면 다음 읽기가 곧바로 최신을 재적재하거나 이미 최신이다. TTL 은 "안전망", 능동 무효화는 "즉시성"을 담당한다.

</details>

---

이전 챕터 → [13. Multiprocessing — CPU 바운드를 진짜 병렬로](13-multiprocessing.md)

다음 챕터 → [15. HTTP Keep-Alive — 연결을 아껴 써라](15-http-keep-alive.md)

돌아가기 → [교과서 목차(README)](README.md)
