# 06. Worker Process — 코어를 다 쓰려면 프로세스를 늘려라

> **한 줄 요약**: 이벤트 루프 하나는 코어 하나만 쓴다(1장 GIL). CPU 를 다 쓰려면 **같은 앱을 여러 프로세스(워커)로** 띄운다 — `uvicorn main:app --workers N`. **Concurrency(한 워커가 async 로 많은 요청을 겹치기)** 와 **Parallelism(여러 워커가 여러 코어에서 진짜 동시 실행)** 은 다른 축이다.

**이 챕터의 키워드**: Worker, Worker Pool, Concurrency vs Parallelism

---

## 1. 왜 중요한가

5장에서 배운 Uvicorn 은 기본적으로 **이벤트 루프 1개 = 프로세스 1개 = 코어 1개**다. 서버 머신에 코어가 8개여도 워커가 1개면 **7개는 논다**. 1장에서 봤듯 GIL 때문에 한 프로세스 안에서는 스레드를 늘려도 CPU 병렬이 안 된다 — CPU 를 다 쓰는 유일한 방법은 **프로세스를 늘리는 것**이다. 하지만 "워커 = 무조건 많이"가 아니다. 워커마다 메모리·DB 커넥션이 N배로 불어난다(7장 커넥션풀로 이어짐). "몇 개가 적정인가", "우리 Cloud Run 배포에선 왜 워커를 안 늘리고 인스턴스를 늘리나"까지가 이 장의 목표다.

## 2. 개념 — 비유로 시작

**비유**: 5장까지의 주방(요리사 1명 + 능숙한 순회 = 이벤트 루프)을 떠올리자.
- **Concurrency(동시성)** = **요리사 한 명**이 냄비 여러 개를 오가며 "기다림이 생길 때마다 다른 냄비로" 겹쳐 처리하는 것. 냄비가 100개여도 한 명으로 감당한다 — 단, **한 순간에 실제로 칼질하는 손은 하나**다(대기가 겹칠 뿐).
- **Parallelism(병렬성)** = **주방(=프로세스)을 여러 개** 차려 요리사를 여러 명 두는 것. 이제 **여러 손이 진짜 동시에** 칼질한다 — CPU 코어를 여러 개 쓴다.
- **Worker** = 그 각각의 독립 주방. `--workers 2` 면 **똑같은 메뉴판(앱)을 가진 주방 2개**가 같은 문(포트)으로 들어온 손님을 나눠 받는다.

같은 async 서버라도 이 둘은 **다른 축**이다: 한 워커 안에서 대기를 겹치는 게 Concurrency, 워커를 늘려 코어를 나눠 쓰는 게 Parallelism. I/O 대기가 많은 서버는 Concurrency 만으로도 멀리 가고, CPU 계산이 많으면 Parallelism(워커/프로세스)이 필요하다.

**정확한 정의**:
- **Worker**: 같은 ASGI 앱을 로드한 **독립 프로세스**. 각자 **자기 이벤트 루프·자기 GIL·자기 메모리**를 갖는다(1장의 "멀티프로세스는 각자 GIL"). Uvicorn `--workers N` 은 마스터 프로세스가 N 개 워커를 `spawn`(Windows)/`fork`(Linux) 해 만들고, OS 가 들어온 연결을 워커들에 분배한다.
- **Concurrency**: 하나의 실행 흐름이 여러 작업을 **번갈아**(양보 지점마다) 진행 — "동시에 진행 중"이지만 순간 실행은 하나. (모듈 1 의 이벤트 루프.)
- **Parallelism**: 여러 실행 흐름이 **물리적으로 같은 시각에** 실행 — 코어가 여러 개여야 성립. (여러 워커/프로세스.)

## 3. 그림

```
Concurrency (워커 1개, 이벤트 루프 1개):
  코어1: [req A][req C][req A][req B]...   ← 대기마다 갈아타며 '겹침'. 순간 실행은 1개
  코어2~8: (놀고 있음)                     ← 이 프로세스는 코어 하나만 씀 (GIL)

Parallelism (워커 4개 = 프로세스 4개):
  코어1: [워커1: 루프 + 그 안의 concurrency]
  코어2: [워커2: 루프 + 그 안의 concurrency]   ← 진짜 동시에 4개 코어
  코어3: [워커3: ...]
  코어4: [워커4: ...]
   ↑ 마스터가 8080 포트를 공유, OS 가 연결을 워커들에 분배

  => Concurrency(축1: 한 워커가 대기를 겹침) 와 Parallelism(축2: 워커로 코어 확장) 은 곱해진다.
     "워커 N개 × 각 워커의 async 동시성" 이 총 처리량.
```

## 4. 직접 돌려보자

### os.getpid() 로 "정말 여러 프로세스가 뜨는가" 확인

`/pid` 는 자기 프로세스의 PID 를 반환한다. `--workers 2` 로 띄우고 여러 번 때리면 **PID 두 종류**가 번갈아 나와야 한다(= 워커 수만큼 프로세스가 떴다는 증거).

앱: [`examples/06_pid_app.py`](examples/06_pid_app.py) · 드라이버: [`examples/06_run_workers.py`](examples/06_run_workers.py)

```python
# 06_pid_app.py
@app.get("/pid")
def pid():
    return {"pid": os.getpid()}   # 이 요청을 처리한 프로세스의 PID
```

드라이버가 하는 일: `uvicorn 06_pid_app:app --workers 2` 를 하위 프로세스로 띄우고 → 뜰 때까지 폴링 → `/pid` 를 10번 호출 → PID 를 집계 → 프로세스 트리 종료.

실행:
```bash
uv run --with fastapi --with uvicorn --with httpx python examples/06_run_workers.py
```

실제 출력:
```
INFO:     Uvicorn running on http://127.0.0.1:8123 (Press CTRL+C to quit)
INFO:     Started parent process [7300]
INFO:     Started server process [27232]
INFO:     Application startup complete.
INFO:     Started server process [23496]
INFO:     Application startup complete.
...
요청  1 → PID 27232
요청  2 → PID 27232
요청  3 → PID 27232
요청  4 → PID 23496
요청  5 → PID 27232
요청  6 → PID 27232
요청  7 → PID 23496
요청  8 → PID 23496
요청  9 → PID 23496
요청 10 → PID 23496

등장한 PID 종류: 2개
  PID 27232: 5회
  PID 23496: 5회
=> 워커 수만큼(2개) 서로 다른 프로세스가 요청을 나눠 처리했다.
```

**어디를 보라**:
- **`Started parent process [7300]`** = 마스터(요청을 직접 처리하지 않고 워커를 관리). 그 아래 **`Started server process [27232]`**, **`[23496]`** = 실제 워커 2개. 마스터+워커 = **3개 프로세스**가 떴다.
- **`Application startup complete`** 이 **두 번** — lifespan(4장)의 startup 이 **워커마다 각각** 실행된다. 즉 엔진·세션팩토리·genai 클라이언트도 **워커 수만큼** 만들어진다(← 이게 곧 "워커 늘리면 자원 N배"의 실체다).
- **요청별 PID 가 `27232` 와 `23496` 두 종류**로 갈렸고 정확히 5회씩. 같은 포트(8123)로 들어온 요청을 **OS 가 두 워커에 나눠** 줬다는 증거. 워커는 각자 **독립 프로세스 = 독립 GIL** 이라, 만약 CPU 계산이었다면 두 코어에서 **진짜 동시(Parallelism)** 로 돌았을 것이다(1장 (a)의 "프로세스 2개 → 절반 시간"과 동일 원리).

> 정리: `--workers N` 은 "같은 앱을 N개 프로세스로". PID 가 갈리는 것이 그 물리적 증거이고, startup 이 N번 도는 것이 그 비용(자원 N배)의 증거다.

## 5. 우리 코드 / 배포와 연결

- **우리 Dockerfile 은 단일 워커다**: [`Dockerfile`](../../Dockerfile#L24) 의 마지막 줄은 `exec uvicorn main:app --host 0.0.0.0 --port ${PORT}` — **`--workers` 옵션이 없다**(기본 1). 즉 컨테이너 1개당 **워커 1개**다. 이건 실수가 아니라 **Cloud Run 전략**이다.
- **왜 워커 대신 인스턴스로 스케일하나**: Cloud Run 은 트래픽에 따라 **컨테이너 인스턴스를 자동으로 늘린다(오토스케일)**. 각 인스턴스가 워커 1개면, "Parallelism(코어 확장)"을 **컨테이너 복제**가 담당한다. 컨테이너 안에서 워커를 N개 띄우는 것보다 (1) 인스턴스 단위로 스케줄/모니터링이 단순하고, (2) 한 워커가 죽어도 인스턴스째 교체되며, (3) `Application startup complete`(=자원 초기화)가 인스턴스마다 딱 한 번이라 자원 회계가 명확하다. 우리 통화 서버는 **I/O 대기 위주(WebSocket·Gemini 스트림)** 라, 한 워커의 async **Concurrency** 만으로도 인스턴스당 다수 통화를 감당한다 — 그래서 인스턴스당 워커 1이 합리적이다.
- **자원 N배의 실측 근거**: 위 (4)에서 startup 이 워커마다 돈다고 했다. 만약 컨테이너 안에서 `--workers 4` 로 갔다면 **엔진·커넥션풀·genai 클라이언트가 4벌** 생겨 메모리와 **DB 커넥션이 4배**가 된다. 우리 DB 는 pgbouncer(6543) 뒤라 커넥션이 유한하니(7장), 워커 남발은 곧 커넥션 고갈로 이어진다. 단일 워커 + 수평 스케일이 이 함정을 피한다.
- **12장 예고**: 한 컨테이너에서 굳이 여러 워커를 관리해야 할 때는 **Gunicorn(프로세스 매니저) + Uvicorn 워커 클래스** 조합을 쓴다(graceful restart·워커 재시작 정책). 우리는 Cloud Run 오토스케일로 대체하지만, 그 대안 구조는 12장에서 다룬다.

## 6. 흔한 오해 / 함정

- ❌ **"워커를 늘리면 무조건 빨라진다."** → 아니다. **메모리 N배, DB 커넥션 N배**(위 (4) startup N회가 증거), 문맥전환·캐시 경합(10장) 증가. I/O 대기 위주 서버는 워커보다 **한 워커의 async Concurrency** 로 훨씬 싸게 처리량을 얻는다.
- ❌ **"코어보다 워커를 훨씬 많이 두면 더 빠르다."** → 코어 수를 넘는 워커는 서로 CPU 를 뺏어 문맥전환만 늘린다. 통념은 **CPU 바운드면 ≈코어 수**, **I/O 바운드면 코어 수 근처에서 소폭 조정**.
- ❌ **"async 를 쓰면 워커가 필요 없다."** → async 는 **Concurrency**(대기 겹치기)만 준다. **CPU 병렬(Parallelism)** 은 여전히 워커/프로세스가 필요(1장 GIL). 두 축은 곱해진다.
- ❌ **"워커끼리 메모리를 공유한다."** → 아니다. 각 워커는 **독립 프로세스**라 전역 변수·캐시·`app.state` 를 공유하지 않는다(그래서 (4)에서 startup 이 워커마다 돈다). 공유가 필요하면 Redis 등 외부 저장소(14장).
- ⚠️ **`--workers` 와 `--reload` 는 함께 쓰지 않는다**(개발 reload 는 단일 프로세스 전제).

## 7. 요약

- 이벤트 루프 1개 = 프로세스 1개 = **코어 1개**(GIL). CPU 를 다 쓰려면 **워커(프로세스)를 늘린다**: `uvicorn ... --workers N`.
- **Concurrency**(한 워커가 async 로 대기를 겹침) 와 **Parallelism**(여러 워커가 여러 코어에서 진짜 동시)은 **다른 축**이고 곱해진다.
- 워커는 **독립 프로세스**: 자기 루프·GIL·메모리·`app.state`. lifespan startup 이 **워커마다** 실행 → 자원(메모리·DB 커넥션) **N배**.
- 워커 남발은 메모리·커넥션 고갈. 권장: CPU 바운드 ≈코어 수, I/O 바운드는 async 로 벌고 워커는 코어 수 근처.
- **우리 배포**: Dockerfile 은 워커 1개, Cloud Run **인스턴스 오토스케일**로 수평 확장(단일 워커 + 수평 스케일). 컨테이너 내 다중 워커는 12장(Gunicorn+Uvicorn).

## 8. 연습문제

1. `06_run_workers.py` 를 `--workers 3` 으로 바꾸면 출력의 "등장한 PID 종류"와 `Application startup complete` 횟수는 각각 어떻게 될까?
2. 코어 4개 머신에서, (a) CPU 계산이 대부분인 서비스와 (b) 외부 API 대기가 대부분인 서비스의 워커 수를 각각 어떻게 잡는 게 합리적일까? 이유는?
3. 우리 Dockerfile 이 `--workers` 를 안 붙이고 단일 워커로 둔 이유를, Cloud Run 오토스케일과 "자원 N배" 관점에서 설명하라.

<details>
<summary>답</summary>

1. "PID 종류"는 **3개**(워커 3개), `Application startup complete` 도 **3번**(워커마다 lifespan startup). 마스터까지 하면 프로세스는 4개.
2. (a) CPU 바운드는 **≈4개**(코어 수). 워커가 각 코어를 하나씩 맡아 진짜 병렬(Parallelism). 코어보다 많으면 문맥전환만 늘어 손해. (b) I/O 바운드는 **코어 수 근처(4개 안팎)** 로 두되, 처리량 대부분은 **한 워커의 async Concurrency**(대기 겹치기)에서 나온다 — 대기가 많아 코어를 오래 안 쓰므로 워커를 과하게 늘릴 필요가 적다.
3. Cloud Run 이 트래픽에 따라 **컨테이너 인스턴스를 오토스케일**하므로 Parallelism(코어 확장)은 인스턴스 복제가 담당한다. 컨테이너 안에서 워커를 N개 두면 엔진·커넥션풀·genai 가 N벌 생겨 **메모리·DB 커넥션이 N배**(startup N회)가 되고 pgbouncer 커넥션 고갈 위험이 커진다. 통화 서버는 I/O 대기 위주라 단일 워커의 async 동시성으로 충분해, **단일 워커 + 수평(인스턴스) 스케일**이 자원 회계와 장애 격리 면에서 유리하다.

</details>

---

🎉 **모듈 2 완료!** 이제 요청이 들어오는 경로(**ASGI 규격 → Uvicorn 서버 → 여러 워커**)를 갖췄다.

다음 모듈 → [**07. Connection Pool — 매번 새로 연결하지 마라 (DB·HTTP)**](07-connection-pool.md)

돌아가기 → [교과서 목차(README)](README.md)
