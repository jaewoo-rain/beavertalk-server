# 고성능 FastAPI 백엔드 교과서 🚀

> "FastAPI를 **쓰는** 수준"을 넘어, **왜 특정 설정이 성능을 바꾸는지 설명하고 최적화하는** 수준으로.
> 모든 챕터는 **개념 → 비유 → 그림 → 직접 실행해보는 코드 → 우리 실제 코드(beavertalk)와 연결 → 함정 → 요약/연습**으로 구성됩니다.

## 이 교과서의 사용법
- 각 챕터의 예제는 `docs/learn/examples/` 안에 **실제로 돌아가는 스크립트**로 있습니다.
- 실행은 Windows 기준 `python examples/xx_yy.py` 또는 `uv run --with <패키지> python ...`.
- 순서대로 읽으면 아래층(파이썬 런타임)부터 위층(운영/스케일)까지 한 줄로 이어집니다.

---

## 커리큘럼 (추천 학습 순서 15단계)

### 모듈 1 — 파이썬은 어떻게 도는가 (동시성의 토대)
| # | 챕터 | 배우는 핵심 키워드 |
|---|---|---|
| 01 | **GIL** — 왜 파이썬 스레드는 CPU를 한 번에 하나만 쓰나 | GIL, CPython, Reference Counting, GC, Bytecode, pymalloc |
| 02 | **Event Loop** — 한 스레드로 수천 연결을 다루는 마법 | Event Loop, Non-blocking I/O, Cooperative Multitasking |
| 03 | **Coroutine / async·await / asyncio** — 멈췄다 이어지는 함수 | Coroutine, async, await, Future, Task, asyncio |

### 모듈 2 — 웹서버가 요청을 받는 법
| # | 챕터 | 키워드 |
|---|---|---|
| 04 | **ASGI vs WSGI** — FastAPI가 서 있는 땅 | ASGI, WSGI, Starlette, Lifespan, Middleware, Dependency Injection, Background Task |
| 05 | **Uvicorn** — 이벤트 루프를 서버로 | Uvicorn, Hypercorn |
| 06 | **Worker Process** — 코어를 다 쓰려면 프로세스를 늘려라 | Worker, Worker Pool, Concurrency vs Parallelism |

### 모듈 3 — 자원 재사용과 측정
| # | 챕터 | 키워드 |
|---|---|---|
| 07 | **Connection Pool** — 매번 새로 연결하지 마라 (DB·HTTP) | Connection Pool, Transaction, N+1, Keep-Alive(예고) |
| 08 | **Profiling & 성능 지표** — 느낌 말고 숫자로 | Profiling, Latency, Throughput, RPS/QPS, P95, P99, CPU/Memory Usage |

### 모듈 4 — 부하와 한계
| # | 챕터 | 키워드 |
|---|---|---|
| 09 | **Load Test** — 터지기 전에 터뜨려봐라 | k6, Locust, wrk, ab, Stress/Spike/Soak Test |

### 모듈 5 — 밑바닥 (OS / CPU)
| # | 챕터 | 키워드 |
|---|---|---|
| 10 | **Context Switching & CPU Cache** | Context Switching, Scheduler, Cache Line, Cache Locality, False Sharing, Branch Prediction, Lock Contention |
| 11 | **Epoll** — 이벤트 루프의 진짜 엔진 | Epoll(Linux), Kqueue(macOS), IOCP(Windows), File Descriptor, System Call |

### 모듈 6 — 프로덕션 스케일
| # | 챕터 | 키워드 |
|---|---|---|
| 12 | **Gunicorn + Uvicorn** — 프로세스 매니저 | Gunicorn, Process Manager, Graceful Restart |
| 13 | **Multiprocessing** — CPU 바운드를 진짜 병렬로 | Multiprocessing, Process Pool, 공유메모리 |
| 14 | **Redis Cache** — 안 하는 게 제일 빠른 계산 | Redis, Cache Aside, Write Through/Back, TTL, LRU, Hit/Miss |
| 15 | **HTTP Keep-Alive** — 연결을 아껴 써라 | Keep-Alive, HTTP/1.1·2·3, TCP, Reverse Proxy, Load Balancer |

### 부록 (심화 — 요청 시 추가)
직렬화(orjson/Protobuf), 메시지 큐(Kafka/RabbitMQ/Redis Streams), 컨테이너/K8s(Docker, Pod, HPA), 모니터링(Prometheus/Grafana/OpenTelemetry/Jaeger), 분산 시스템(Sharding/Replication/Consistent Hashing).

---

## 진행 상태
- [x] 모듈 1 (Ch 01–03) — GIL / Event Loop / Coroutine·asyncio
- [x] 모듈 2 (Ch 04–06) — ASGI vs WSGI / Uvicorn / Worker Process
- [x] 모듈 3 (Ch 07–08) — Connection Pool / Profiling·지표
- [x] 모듈 4 (Ch 09) — Load Test (Locust/k6)
- [x] 모듈 5 (Ch 10–11) — Context Switching·CPU Cache / Epoll
- [x] 모듈 6 (Ch 12–15) — Gunicorn+Uvicorn / Multiprocessing / Redis / Keep-Alive

**🎉 전 15장 완주 — 커리큘럼 본편 완성!**
