# 08. Profiling & 성능 지표 — 느낌 말고 숫자로

> **한 줄 요약**: "느리다"는 **느낌**이 아니라 **측정**이다. 최적화 전에 **프로파일링으로 병목(hotspot)을 먼저 찾아라**. 그리고 **평균(mean)은 거짓말을 한다** — 사용자가 실제로 겪는 고통은 **꼬리 지연 P95/P99** 에 있다.

**이 챕터의 키워드**: Profiling, Latency, Throughput, RPS/QPS, P95, P99, CPU Usage, Memory Usage

> 이전 챕터([07. Connection Pool](07-connection-pool.md))에서 "자원을 아껴 쓰면 빨라진다"를 봤다. 그런데 **정말 빨라졌는지, 어디가 느린지는 어떻게 아나?** 이 장이 그 도구다.

---

## 1. 왜 중요한가

우리 서버는 통화 1건이 5분간 오디오를 실시간 중계하고, 통화가 끝나면 **여러 번의 LLM 호출**(분석 → 표현 추출 → 표현마다 TTS 합성 → Storage 업로드)을 줄줄이 돈다. 언젠가 "통화후 분석이 느리다"는 말이 나올 것이다. 그때 **감으로 아무 데나 고치면 십중팔구 틀린 곳을 고친다.**

- "리스트 순회가 느린 것 같아" → 실제 병목은 LLM 네트워크 대기였다. 리스트를 아무리 최적화해도 0.1% 도 안 빨라진다.
- "평균 응답이 50ms 니까 괜찮아" → 그런데 100명 중 1명은 700ms 를 겪고 있다. 그 1명이 이탈한다.

**측정 없는 최적화는 미신(superstition)이다.** 이 장은 (1) 병목을 **cProfile 로 찾고**, (2) 지연을 **분포로 보고(P95/P99)**, (3) **지연 ≠ 처리량**을 숫자로 구분하는 법을 다룬다.

## 2. 개념 — 비유로 시작

**비유 1 (병목 찾기)**: 수도관 여러 개가 이어진 배관에서 물이 느리다. 모든 관을 다 굵게 갈면 돈 낭비다. **가장 좁은 관 하나(bottleneck)** 만 찾아 갈면 된다. 프로파일러는 "어느 관이 제일 좁은지" 재는 유량계다. 눈으로 관을 노려봐선 못 찾는다 — 재야 한다.

**비유 2 (평균의 함정)**: 식당 리뷰 평점이 4.0 이라 갔더니, 알고 보니 손님 95명이 별 5개, 5명이 별 1개였다. "평균 4.0"은 그 5명이 겪은 최악을 **숨긴다**. 서버도 똑같다. **평균 응답 50ms** 뒤에 "가끔 700ms 나는 5%"가 숨어 있다. 그 5% 가 바로 **P95/P99**(상위 5%/1% 경계)다.

**정확한 정의**:
- **Latency(지연)**: 요청 **하나**가 시작~끝까지 걸리는 시간. 단위 ms. "이 요청 얼마나 오래 걸렸나."
- **Throughput(처리량)**: **단위 시간당 처리한 요청 수**. RPS(Requests/sec) 또는 QPS(Queries/sec). "초당 몇 개 쳐냈나."
- **P95 / P99**: 지연을 오름차순 정렬했을 때 **95%/99% 지점의 값**. "P99=700ms" = 요청의 99% 는 700ms 이내, **1% 는 그보다 나쁘다**. 꼬리 지연(tail latency).
- **CPU Usage / Memory Usage**: 그 처리를 하느라 쓴 코어 시간·메모리. 병목이 CPU 인지 대기인지 메모리인지 가른다.

> ⚠️ **Latency 와 Throughput 은 다른 축이다.** 지연을 못 줄여도 동시성을 올리면 처리량은 오른다(4-(c)에서 증명). 반대로 처리량을 위해 배치를 키우면 개별 지연은 나빠질 수 있다. 둘을 한 숫자로 뭉뚱그리지 마라.

## 3. 그림

```
[ 병목(hotspot) — 프로파일러가 재는 것 ]
  요청 처리 = f() -> g() -> h()
     f: ▏ 2ms
     g: ██████████████████ 180ms   <- 여기다. 나머지 다 합쳐도 3ms
     h: ▏ 1ms
  => g 만 고쳐라. f/h 를 최적화하는 건 미신.

[ 평균의 함정 — 분포로 봐야 하는 이유 ]
  지연 분포:
    빠름 ██████████████████████████████████████████ 95%  (~20ms)
    느림 ██                                          5%   (300~800ms)
    mean = 50ms  <- "괜찮아 보인다"
    P99  = 700ms <- 100명 중 1명의 현실 (숨어 있던 고통)

[ Latency vs Throughput — 다른 축 ]
  동시성 1  : [50ms][50ms][50ms]...        RPS 낮음, 지연 50ms
  동시성 10 : [50ms]x10 겹침               RPS 10배, 지연 그대로 50ms
  => 지연은 그대로인데 throughput 만 오른다 (Little's Law)
```

## 4. 직접 돌려보자

### (a) cProfile 로 병목 찾기 — "어느 함수가 시간을 먹나"

파일: [`examples/08_profile_hotspot.py`](examples/08_profile_hotspot.py)

일부러 두 비효율을 심었다: **list 로 멤버십 검사**(`x in list` = O(n)), **문자열 `+=` 누적**(매번 새 문자열). cProfile 로 돌려 `pstats` 로 `tottime`(그 함수 자체가 쓴 시간) 상위를 출력한다.

```python
def slow_membership(items: list[int], probes: list[int]) -> int:
    hits = 0
    for p in probes:
        if p in items:   # <- 병목: list.__contains__ 는 매번 처음부터 훑음(O(n))
            hits += 1
    return hits
```

실행:
```bash
python examples/08_profile_hotspot.py
```

실제 출력:
```
===== BEFORE  (list in / str +=) =====
         4 function calls in 1.824 seconds
   Ordered by: internal time
   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
        1    1.821    1.821    1.821    1.821 08_profile_hotspot.py:22(slow_membership)
        1    0.002    0.002    1.824    1.824 08_profile_hotspot.py:39(workload)
        1    0.001    0.001    0.001    0.001 08_profile_hotspot.py:31(slow_concat)

===== AFTER   (set in / join) =====
         5 function calls in 0.004 seconds
   Ordered by: internal time
   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
        1    0.003    0.003    0.004    0.004 08_profile_hotspot.py:59(workload_fixed)
        1    0.000    0.000    0.000    0.000 08_profile_hotspot.py:47(fast_membership)
```

**어디를 보라**:
- **`tottime` 열이 범인을 지목한다**: `slow_membership` 이 **1.821s** 로 전체 1.824s 의 **99.8%** 를 혼자 먹었다. 만약 감으로 `slow_concat`(0.001s)을 최적화했다면? 0.05% 도 안 빨라졌을 것이다. **프로파일러가 없으면 이 사실을 모른다.**
- **고친 뒤**: `list` → `set` 으로 바꾸자 멤버십이 O(n)→O(1) 이 되어 전체가 **1.824s → 0.004s (약 450배)**. 진짜 병목 하나만 고쳤을 뿐이다.
- `tottime` vs `cumtime`: **tottime** 은 그 함수 **자체**가 쓴 시간(하위 호출 제외), **cumtime** 은 그 함수 + 그 함수가 부른 것들 **누적**. "누가 직접 시간을 쓰나"는 tottime, "이 진입점이 총 얼마 걸리나"는 cumtime 으로 본다.

> 순서: **프로파일 → 상위 tottime 확인 → 그것만 고침 → 다시 프로파일로 개선 확인.** 추측은 이 루프 어디에도 없다.

### (b) 평균의 함정 — mean 은 좋은데 P99 는 처참한

파일: [`examples/08_latency_percentiles.py`](examples/08_latency_percentiles.py)

"대부분 빠르고(95%, ~20ms) 가끔 느린(5%, 300~800ms)" 현실적 분포를 만들어 mean/median/P95/P99 를 `statistics` 로 계산한다.

실행:
```bash
python examples/08_latency_percentiles.py
```

실제 출력:
```
샘플 수        : 1000
mean (평균)    :    50.1 ms   <- '좋아 보인다'
median (중앙)  :    20.2 ms   <- 절반은 이보다 빠르다
P95            :   370.4 ms
P99            :   702.3 ms   <- 100명 중 1명이 겪는 최악
max            :   799.8 ms
P99 / median   :    34.7 x  (꼬리가 중앙의 몇 배인가)

분포 (ASCII 히스토그램):
     6~    85ms | ################################################## 942
    85~   165ms |  0
   165~   244ms |  0
   244~   324ms |  1
   324~   403ms |  7
   403~   482ms | # 14
   482~   562ms | # 11
   562~   641ms | # 12
   641~   720ms |  6
   720~   800ms |  7
```

**어디를 보라**:
- **mean 50ms, median 20ms** — 여기까지만 보면 "빠른 서버"다. 그런데 **P99 는 702ms**. 중앙값의 **34.7배**다. 평균은 이 꼬리를 **완전히 숨겼다**.
- 히스토그램이 보여주는 진실: 942개(94%)는 왼쪽 맨 끝(빠름)에 뭉쳐 있고, 나머지 58개가 오른쪽으로 길게 흩어진 **롱테일(long tail)**. 사용자 100명이 접속하면 그중 1명은 매번 700ms 를 본다.
- **왜 P99 가 중요한가**: 한 페이지가 내부적으로 API 를 20번 부르면, "요청당 P99=1%"라도 **한 페이지가 느릴 확률은 1-(0.99^20) ≈ 18%**. 꼬리는 **곱해지며 증폭**된다. 그래서 큰 서비스는 평균이 아니라 P99(때론 P999)로 SLA 를 건다.

### (c) Latency vs Throughput — 지연은 그대로, 처리량은 오른다

파일: [`examples/08_latency_vs_throughput.py`](examples/08_latency_vs_throughput.py)

각 "요청"은 50ms I/O 대기(`await asyncio.sleep`)다. 동시성만 1→200 으로 바꿔가며 총 시간·RPS·요청당 지연을 잰다. 모듈 1(GIL: I/O 는 대기중 양보)·모듈 2(event loop: 한 스레드로 겹침)의 직접 연장이다.

실행:
```bash
python examples/08_latency_vs_throughput.py
```

실제 출력:
```
작업: 50ms I/O 요청 200개를, 동시성만 바꿔가며 처리
concurrency=  1 | 총 12.35s | RPS=   16.2 | 요청당 지연 평균= 61.7ms
concurrency= 10 | 총  1.24s | RPS=  161.2 | 요청당 지연 평균= 61.9ms
concurrency= 50 | 총  0.25s | RPS=  808.7 | 요청당 지연 평균= 61.5ms
concurrency=200 | 총  0.06s | RPS= 3220.0 | 요청당 지연 평균= 61.1ms
```

**어디를 보라**:
- **요청당 지연은 ~61ms 로 거의 불변**이다(동시성이 200배가 돼도!). 개별 요청은 여전히 자기 50ms 를 기다린다 — 지연은 안 줄었다. (50 이 아니라 61ms 인 건 Windows 타이머 해상도가 거칠어서다. 미세 벤치의 흔한 현상.)
- 그런데 **RPS 는 16 → 161 → 808 → 3220 으로 동시성에 비례해 상승**한다. 대기가 **겹치기(overlap)** 때문이다. 한 요청이 `await` 로 자는 동안 이벤트 루프가 다른 요청을 진행시킨다.
- **Little's Law**: `평균 동시처리 ≈ 도착률(RPS) × 평균 지연(s)`. 뒤집으면 `RPS ≈ concurrency / 지연`. 여기서 `200 / 0.06 ≈ 3300` — 실측 3220 과 거의 맞는다. **이것이 "우리 서버가 한 스레드로 수천 연결을 처리한다"의 산수적 근거**다.
- 교훈: **"느리다"가 지연 문제인지 처리량 문제인지 먼저 가려라.** 지연이 문제면 병목 함수를(=(a)), 처리량이 문제면 동시성/워커를(=6장) 손봐야 한다. 처방이 정반대다.

### (d) tracemalloc — 메모리를 '어느 줄'이 먹나

파일: [`examples/08_tracemalloc_top.py`](examples/08_tracemalloc_top.py)

CPU 프로파일러가 '시간 먹는 함수'를 찾듯, `tracemalloc` 은 '메모리 먹는 **줄**'을 찾는다. 크기가 다른 세 버퍼를 만들고 할당 상위 라인을 출력한다.

실행:
```bash
python examples/08_tracemalloc_top.py
```

실제 출력:
```
메모리 할당 상위 5개 라인:
   19686.6 KiB  (499744 blocks)  08_tracemalloc_top.py:19
   10971.0 KiB  (149725 blocks)  08_tracemalloc_top.py:30
    1319.1 KiB  (     1 blocks)  08_tracemalloc_top.py:25

현재 추적 메모리:  31.23 MiB   피크:  31.23 MiB
```

**어디를 보라**:
- 19번 줄(`[i for i in range(500_000)]`)이 **19.6 MiB, 약 50만 블록** — int 객체 하나하나가 별도 할당이라 블록 수가 폭발한다.
- 30번 줄(작은 dict 5만 개)이 10.9 MiB, **14.9만 블록** — dict 하나당 여러 내부 객체(키·값 str·int)라 블록이 많다. **작은 객체 대량 생성**이 메모리·GC 압력의 주범임을 보여준다.
- **25번 줄(`bytearray` 누적)은 1.3 MiB 인데 블록이 딱 1개**다. 128만 바이트를 **하나의 연속 버퍼**에 담았기 때문. → **`bytearray` 로 누적하는 것이 작은 조각 수만 개보다 훨씬 메모리·할당 효율적**이라는, 우리 통화 오디오 버퍼 관용구의 근거다(아래 5절).

## 5. 우리 코드와 연결

- **관측을 코드에 심는다 — duration 로깅**: [`call_session.py`](../../domains/learning/realtime/call_session.py#L269) 의 `_persist_remaining` 은 통화가 끝날 때 `duration_s = int(loop.time() - state.call_start_ts)` 를 계산해 `"저장 완료 ... duration=%ds"` 로 남긴다. 이게 바로 **프로덕션 latency 지표를 코드에 심어 두는** 실무 형태다 — 나중에 로그에서 `duration` 을 긁어 P95/P99 를 뽑을 수 있다. `perf_counter` 가 아니라 `loop.time()` 을 쓰는 건 이벤트 루프의 단조시계(monotonic)라 벽시계 보정에 안 흔들리기 때문.
- **멀티콜이 왜 P99 를 키우나 — 분석 캐스케이드**: [`normalcall_service.analyze_call`](../../domains/learning/service/normalcall_service.py#L324) 은 (1) LLM 분석 1회 → (2) 표현마다 `tts.synthesize_korean` → (3) `storage.upload` → (4) `run_db` 로 URL 저장을, **표현 개수만큼 직렬(for 루프)로** 돈다. 직렬 체인의 전체 지연은 **각 단계 지연의 합**이고, 한 단계라도 꼬리(예: TTS 가 가끔 느림)를 물면 **전체 P99 가 그 꼬리에 지배**된다. (b)의 "꼬리는 곱해지며 증폭"이 여기 그대로 적용된다. 표현 10개면 느릴 확률이 10배로 노출된다. → 개선 방향은 독립적인 TTS 를 `asyncio.gather` 로 겹치는 것(=(c)의 throughput 논리)이지만, 외부 API rate limit 과 저울질해야 한다.
- **동기 DB 를 스레드풀로 — 지연을 숨기는 게 아니라 겹치는 것**: [`run_db`](../../domains/learning/service/normalcall_service.py#L50) 는 동기 psycopg2 쿼리를 `run_in_threadpool` 로 넘긴다. DB 쿼리 자체의 latency 는 안 줄지만(=(c)의 개별 지연 불변), 그 대기 동안 이벤트 루프가 다른 통화를 진행시켜 **전체 throughput 을 지킨다**. 만약 루프 스레드에서 직접 동기 쿼리를 하면 그 대기 시간만큼 **모든 통화의 P99 가 함께 나빠진다**.
- **bytearray 버퍼 — 메모리 지표의 관용구**: `_CallState.cur_user_pcm = bytearray()` 처럼 오디오를 **하나의 bytearray 에 누적**하는 우리 패턴은 (d)에서 본 "1 block vs 수만 block"의 실전판이다. 20ms 청크를 리스트에 쌓아 매번 새 bytes 를 만들면 할당·GC 가 폭증한다.
- **미들웨어로 처리시간 기록(개념, 10장/모니터링 예고)**: FastAPI 는 요청마다 `time.perf_counter()` 로 감싸 `X-Process-Time` 헤더나 로그를 남기는 미들웨어를 흔히 둔다. 지금 우리는 통화 `duration` 만 남기지만, 일반 엔드포인트에도 이런 미들웨어를 붙이면 **엔드포인트별 P95/P99** 를 뽑을 수 있다 — 부하 테스트(9장)와 모니터링에서 다시 나온다.

## 6. 흔한 오해 / 함정

- ❌ **평균만 보기(꼬리 무시).** (b)처럼 mean 50ms 뒤에 P99 700ms 가 숨는다. **항상 P95/P99(가능하면 히스토그램)까지** 보라. 평균은 이상치(outlier) 몇 개에 끌려다니거나, 반대로 이상치를 희석해 숨긴다.
- ⚠️ **프로파일링 오버헤드로 절대시간이 왜곡된다.** cProfile 은 **함수 호출마다** 후킹하므로, 호출이 많은 코드일수록 부풀려진다. 실측(같은 코드, 300만 번 함수 호출):
  ```
  raw wall      : 0.128s
  under cProfile: 0.442s
  overhead      : 3.45x   <- 호출 많을수록 왜곡 큼
  ```
  → cProfile 의 **절대 시간을 진짜 성능으로 믿지 마라.** "A 가 B 보다 몇 배"라는 **상대 비교**로만 읽어라. 절대 시간이 필요하면 프로파일러 밖에서 `perf_counter` 로 따로 재라(또는 샘플링 프로파일러 `py-spy` 를 쓰라 — 오버헤드가 훨씬 작다).
- ⚠️ **micro-benchmark 함정(워밍업/캐시).** 첫 실행은 임포트·JIT 없음·캐시 미스로 느리다. 반복 측정하고 첫 회를 버리거나 `timeit` 처럼 여러 번 돌려 최솟값/중앙값을 봐라. (c)의 지연이 50 이 아니라 61ms 로 나온 것도 OS 타이머 해상도라는 환경 요인 — **환경을 기록하라**.
- ❌ **프로덕션과 다른 데이터로 측정.** 100건짜리 장난감 데이터로는 O(n²) 병목이 안 보인다. (a)의 list 멤버십도 N 이 작으면 티가 안 난다. **실제와 비슷한 규모·분포**로 재라.
- ❌ **"최적화 먼저 코딩"(측정 없이 미리 꼬기).** 읽기 어려운 마이크로 최적화를 병목도 아닌 곳에 미리 넣지 마라. **먼저 정확·명료하게 짜고, 프로파일로 병목을 확인한 뒤, 그곳만** 고쳐라. (a)에서 봤듯 병목은 대개 코드의 극히 일부다.

## 7. 요약

- **측정 없는 최적화는 미신.** `cProfile` + `pstats` 로 **tottime 상위 함수(hotspot)** 를 먼저 찾고, 그것만 고치고, 다시 프로파일해 확인하라.
- **Latency ≠ Throughput.** 지연은 요청 하나의 시간, 처리량(RPS/QPS)은 초당 개수. 동시성을 올리면 **지연 그대로 처리량만** 오른다(Little's Law: RPS ≈ concurrency / 지연).
- **평균은 거짓말한다.** mean/median 뒤에 숨은 **P95/P99 꼬리 지연**을 보라. 꼬리는 여러 호출에 걸쳐 **곱해지며 증폭**된다.
- **메모리는 `tracemalloc` 으로 '어느 줄'인지** 지목한다. 작은 객체 대량 생성은 블록·GC 폭증 — `bytearray` 누적이 효율적.
- cProfile 절대시간은 **상대 비교**로만. 워밍업·데이터 규모·환경을 기록하라.

## 8. 연습문제

1. `08_profile_hotspot.py` 의 `pstats` 정렬을 `sort_stats("tottime")` 대신 `sort_stats("cumtime")` 로 바꾸면 `workload` 가 위로 올라온다. 왜 tottime 에선 `slow_membership` 이, cumtime 에선 `workload` 가 1등일까?
2. (b)에서 만약 느린 5% 를 300~800ms 가 아니라 **50~60ms** 로 바꾸면 mean 과 P99 는 어떻게 될까? "평균이 거짓말하는" 정도가 왜 줄어드나?
3. (c)에서 요청당 작업이 `asyncio.sleep`(I/O) 이 아니라 **CPU 계산**이었다면, 동시성을 200 으로 올려도 RPS 가 안 오르는 이유를 1장(GIL)으로 설명하라.
4. 우리 `analyze_call` 의 표현별 TTS 루프를 `asyncio.gather` 로 병렬화하면 어떤 지표가 좋아지고(latency? throughput?), 어떤 위험(외부 API)이 생기나?

<details>
<summary>답</summary>

1. **tottime** 은 그 함수가 **직접** 쓴 시간이다. 실제 루프를 도는 건 `slow_membership` 이라 그 자체 시간이 크다. **cumtime** 은 하위 호출 누적이라 `workload`(= slow_membership + slow_concat 을 부름)가 그 둘의 합을 품어 1등이 된다. "누가 직접 태우나"는 tottime, "이 진입점 총량"은 cumtime.
2. 느린 꼬리가 50~60ms 로 줄면 mean 도 median 도 P99 도 전부 20~60ms 안에 모인다. **분포가 좁아져** P99/median 배수가 1에 가까워진다. 평균이 거짓말하는 정도 = **분포의 넓이(꼬리 길이)** 에 비례한다. 꼬리가 짧으면 평균이 대표값 노릇을 제법 한다.
3. CPU 계산은 `await` 양보 지점이 없어 **GIL 을 놓지 않는다**. 이벤트 루프는 한 스레드라, 한 요청이 계산하는 동안 다른 요청이 진행되지 못한다. 동시성을 올려도 실제로 도는 건 항상 하나 → RPS 가 안 오른다. 이때는 프로세스/워커(6·13장)로 코어를 나눠 써야 한다.
4. **throughput(전체 완료 시간)** 이 좋아진다 — 직렬 합 → 겹침(overlap)으로 총 지연이 준다(개별 TTS 지연 자체는 그대로). 위험: 외부 TTS API 의 **rate limit / 동시요청 한도**를 초과해 429·차단이 날 수 있다. `asyncio.Semaphore` 로 동시성을 제한하며 gather 해야 한다((c)의 sem 패턴).

</details>

---

다음 챕터 → [09. Load Test — 터지기 전에 터뜨려봐라](09-load-test.md)

돌아가기 → [교과서 목차(README)](README.md)
