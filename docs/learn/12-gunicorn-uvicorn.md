# 12. Gunicorn + Uvicorn — 워커를 감독하는 프로세스 매니저

> **한 줄 요약**: 6장에서 `uvicorn --workers N` 으로 워커(프로세스)를 늘렸다. **Gunicorn** 은 그 워커들을 **감독(supervise)** 하는 매니저다 — 죽은 워커 재시작, 무중단 재배포(graceful restart), 워커 재활용(`--max-requests` 로 메모리 누수 방어). 리눅스 프로덕션 표준 조합이 `gunicorn main:app -k uvicorn.workers.UvicornWorker -w 4`. 단, **Gunicorn 은 Linux 전용**이라 우리 Cloud Run 배포는 이걸 **인스턴스 오토스케일**로 대체한다.

**이 챕터의 키워드**: Gunicorn, Process Manager, Graceful Restart, UvicornWorker

---

## 1. 왜 중요한가

6장에서 `--workers 2` 로 프로세스를 늘려 코어를 나눠 쓰는 걸 봤다. 그런데 워커를 **누가 관리하는가**? 워커 하나가 메모리 누수로 부풀거나 예외로 죽으면? 새 코드를 배포할 때 **통화가 끊기지 않게** 워커를 하나씩 갈아끼우려면? Uvicorn 의 `--workers` 도 마스터가 워커를 스폰하긴 하지만, 프로덕션 운영에 필요한 **감독 정책**(재시작·재활용·무중단 재배포·타임아웃 kill)은 **Gunicorn** 이 훨씬 성숙하다.

동시에 이 장은 **정직해야** 한다. 우리 [`Dockerfile`](../../Dockerfile#L24) 은 gunicorn 을 쓰지 **않는다** — 바로 `uvicorn main:app` 단일 프로세스다. 왜 표준 조합을 안 쓰고 단일 워커로 갔는지(→ Cloud Run 오토스케일이 그 일을 대신함)를 이해하는 것이, "언제 gunicorn 이 필요하고 언제 사족인가"를 판단하는 힘이 된다.

> ⚠️ **이 머신(Windows)에선 Gunicorn 을 못 돌린다.** Gunicorn 은 유닉스 `fork()`·시그널·프로세스 그룹에 의존해 **Linux/macOS 전용**이다(공식 문서: "Gunicorn requires a UNIX-like OS. It does not run on Windows"). 그래서 아래 (4)에서 gunicorn 명령·config 는 **보여주기만** 하고 실행하지 않는다. 대신 동일한 개념인 **마스터-워커 감독 트리**를 `uvicorn --workers 2` 로 **실제로 띄워** 증명한다.

## 2. 개념 — 비유로 시작

**비유**: 6장의 "주방 여러 개(워커)"를 떠올리자. 주방을 여러 개 차렸으면 이제 **매니저(점장)** 가 필요하다.

- **Gunicorn = 점장(마스터 프로세스)**. 직접 요리하지 않는다. 하는 일은: 주방(워커)을 정해진 수만큼 열고, **불난 주방(죽은 워커)을 즉시 새로 연다**, 오래 일해 지친 요리사(메모리 부푼 워커)를 **정기적으로 교대**시킨다(`--max-requests`), 메뉴가 바뀌면(재배포) 손님 끊기지 않게 **주방을 하나씩** 새 메뉴로 교체한다(graceful restart).
- **Uvicorn Worker = 각 주방의 요리사**. 실제 요리(ASGI 요청·async 이벤트 루프)를 한다. Gunicorn 이 "요리사를 감독"하려면 그 요리사가 **ASGI 를 아는 요리사**여야 한다 — 그게 `-k uvicorn.workers.UvicornWorker`(워커 클래스 지정)다. 즉 **Gunicorn(감독) + Uvicorn(실제 ASGI 실행)** 의 역할 분담이다.

**정확한 정의**:
- **Process Manager(프로세스 매니저)**: 여러 워커 프로세스를 **낳고(fork/spawn)·감시하고·재시작하는** 상위 프로세스. Gunicorn 마스터가 그것. 요청은 워커가 처리하고, 마스터는 **워커의 생사·수명만** 관리한다.
- **Worker Class(`-k`)**: Gunicorn 은 원래 WSGI(동기) 서버다. **ASGI(async, 4장)** 앱을 돌리려면 워커를 async 로 갈아끼워야 하는데, 그게 `uvicorn.workers.UvicornWorker` — "Gunicorn 이 감독하는 자리에 Uvicorn 이 앉는다".
- **Graceful Restart**: 재배포/리로드 시 **기존 워커에 새 요청을 그만 보내고(연결 드레이닝)**, 처리 중인 요청이 끝나면 종료한 뒤 새 워커를 띄운다. 워커를 하나씩 돌려가며 하면 **다운타임 0** 으로 코드를 교체할 수 있다.

## 3. 그림

```
[Gunicorn 프로세스 매니저 — Linux 프로덕션 표준]

        gunicorn master (PID 100)         ← 요청 처리 X. 워커 감독만
        ├─ signal 처리(HUP=graceful reload, TERM=종료)
        ├─ 죽은 워커 감지 → 재시작
        ├─ max-requests 도달 워커 → 재활용(교대)
        │
        ├── UvicornWorker (PID 101)  코어1  [이벤트 루프 + async concurrency]
        ├── UvicornWorker (PID 102)  코어2  [이벤트 루프 + async concurrency]
        ├── UvicornWorker (PID 103)  코어3  [ ... ]
        └── UvicornWorker (PID 104)  코어4  [ ... ]
              ↑ 하나의 포트를 공유, OS 가 연결을 워커에 분배(6장)

[우리 배포 — Cloud Run: gunicorn 없이 단일 워커 × 인스턴스 N]

   Cloud Run 오토스케일러
   ├── 컨테이너 인스턴스 A:  uvicorn main:app (워커 1)   ← "감독"은 Cloud Run 이
   ├── 컨테이너 인스턴스 B:  uvicorn main:app (워커 1)      죽으면 인스턴스째 교체,
   └── 컨테이너 인스턴스 C:  uvicorn main:app (워커 1)      트래픽 따라 개수 자동 조절
         ↑ "여러 코어/병렬"을 컨테이너 복제가 담당 (6장의 결론)
```

## 4. 직접 돌려보자

### (a) Gunicorn 명령·config — 보여주기만(이 머신선 미실행)

리눅스 프로덕션이라면 이렇게 띄운다(⚠️ **아래는 실행 안 함 — Windows 미지원**):

```bash
# 가장 흔한 한 줄: UvicornWorker 4개를 gunicorn 이 감독
gunicorn main:app \
    -k uvicorn.workers.UvicornWorker \
    -w 4 \
    -b 0.0.0.0:8080 \
    --max-requests 1000 --max-requests-jitter 100 \
    --graceful-timeout 30 --timeout 60
```

config 파일(`gunicorn.conf.py`)로 두면 이렇게 된다:

```python
# gunicorn.conf.py  (역시 이 머신선 미실행 — 개념 예시)
import multiprocessing

bind = "0.0.0.0:8080"
worker_class = "uvicorn.workers.UvicornWorker"
workers = multiprocessing.cpu_count()      # 코어 수 = CPU 병렬 상한(6장·13장)
max_requests = 1000                        # 워커가 1000요청 처리하면 재활용(메모리 누수 방어)
max_requests_jitter = 100                  # 모든 워커가 '동시에' 재활용돼 순단 나는 것 방지
graceful_timeout = 30                      # graceful 종료 최대 대기(초). 넘으면 강제 kill
timeout = 60                               # 워커가 이 시간 응답 없으면 죽었다 보고 재시작
```

각 옵션이 왜 있는지:
- **`-w / workers`**: 워커(프로세스) 수 = 코어 병렬 상한. 통념 CPU 바운드 ≈ 코어 수, I/O 바운드는 그 근처(6장).
- **`--max-requests`**: 워커가 일정 요청을 처리하면 **스스로 종료 → 마스터가 새로 스폰**. 파이썬 장기 실행 프로세스의 **미세한 메모리 누수/단편화**를 주기적 재활용으로 리셋한다. `--max-requests-jitter` 로 워커마다 시점을 흩어 **동시 재활용 순단**을 막는다.
- **`--graceful-timeout`**: 재시작 시 처리 중 요청을 기다려 주는 상한. 이 시간 안에 못 끝내면 강제 종료.
- **`--timeout`**: 워커가 이 시간 동안 마스터에 신호(heartbeat)를 못 주면 **먹통으로 간주해 kill 후 재시작**. ⚠️ **우리처럼 WebSocket 장기 연결/스트리밍**이 있으면 이 값이 너무 짧으면 정상 연결을 죽인다 — async 워커(UvicornWorker)에선 heartbeat 방식이 달라 보통 문제없지만, 동기 워커였다면 치명적이다.

### (b) 개념을 실제로: uvicorn 으로 '마스터-워커 감독 트리' 증명

Gunicorn 을 못 돌리니, **같은 구조**(하나의 마스터가 여러 워커를 낳아 감독)를 `uvicorn --workers 2` 로 띄워 **PID/PPID 관계**로 눈으로 본다.

앱: [`examples/12_pidtree_app.py`](examples/12_pidtree_app.py) · 드라이버: [`examples/12_uvicorn_supervise.py`](examples/12_uvicorn_supervise.py)

```python
# 12_pidtree_app.py — 워커가 자기 pid 와 부모(마스터) ppid 를 반환
@app.get("/who")
def who() -> dict[str, int]:
    return {"pid": os.getpid(), "ppid": os.getppid()}
```

드라이버가 하는 일: `uvicorn 12_pidtree_app:app --workers 2` 를 하위 프로세스로 띄우고 → `/who` 를 20번 호출 → 각 워커의 `(pid, ppid)` 를 모아 → "여러 워커의 ppid 가 **하나의 공통 마스터**로 모이는" 감독 트리를 그린다.

실행:
```bash
uv run --with fastapi --with uvicorn --with httpx python examples/12_uvicorn_supervise.py
```

실제 출력:
```
INFO:     Started parent process [25912]
INFO:     Started server process [10268]
INFO:     Started server process [40484]
INFO:     Application startup complete.
INFO:     Application startup complete.
...
=== 감독 트리(마스터 → 워커) ===
master PID 25912   (요청을 직접 처리하지 않고 워커를 감독)
  └─ worker PID 10268   (5개 요청 처리)
  └─ worker PID 40484   (15개 요청 처리)

워커 수: 2개, 공통 부모(마스터) 수: 1개
=> 워커 여럿의 부모(ppid)가 '하나의 마스터'로 모인다 = 감독 트리.
   Gunicorn 이라면 이 마스터 자리에 gunicorn, 워커 자리에 UvicornWorker 가 앉는다.
```

**어디를 보라**:
- **`Started parent process [25912]`** = 마스터. 이 프로세스는 `/who` 요청을 **직접 처리하지 않는다** — 워커를 낳고 감독만 한다. 출력의 `master PID 25912` 와 정확히 같다.
- 워커 두 개(`10268`, `40484`)의 **부모(ppid)가 둘 다 `25912`** 로 모인다. 이게 감독 트리의 실체다: **하나의 마스터 아래 여러 워커**. Gunicorn 도 정확히 이 모양이고, 다른 점은 마스터가 **재시작·재활용·graceful reload 정책**을 더 갖췄다는 것뿐이다.
- `Application startup complete` 가 **두 번**(워커마다 lifespan startup). 6장에서 본 "워커 수만큼 자원 N배"가 여기서도 그대로다 — gunicorn `-w 4` 면 startup 이 4번, 엔진·커넥션풀·genai 클라이언트가 4벌이다.
- 요청 분배가 5:15 로 **고르지 않다** — OS 커널이 연결을 워커에 분배하는 방식(Windows spawn)상 완벽 균등이 아니다. 부하가 커지면 평준화된다.

> 정리: Uvicorn 의 `--workers` 도 마스터-워커 트리를 만든다. Gunicorn 은 **그 트리 위에 운영 정책(재시작·재활용·무중단 재배포)** 을 얹은 성숙한 프로세스 매니저다.

## 5. 우리 코드 / 배포와 연결

### 우리 Dockerfile 은 gunicorn 이 아니라 바로 uvicorn (단일 워커)

[`Dockerfile`](../../Dockerfile#L24) 의 마지막 줄은:
```dockerfile
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
```
`gunicorn` 도, `--workers` 도 없다. **컨테이너 1개 = uvicorn 1프로세스 = 워커 1개.** 이건 실수가 아니라 **Cloud Run 전략**이다(6장에서 예고). Cloud Run 이 **트래픽에 따라 컨테이너 인스턴스를 오토스케일**하므로, "여러 코어에서 진짜 병렬(Parallelism)"과 "죽은 프로세스 교체"라는 gunicorn 의 두 핵심 역할을 **플랫폼이 인스턴스 단위로** 대신한다. `exec` 는 셸을 거치지 않고 uvicorn 을 **PID 1** 로 만들어, Cloud Run 이 보내는 `SIGTERM` 을 uvicorn 이 직접 받아 **graceful shutdown**(진행 중 통화 정리)할 수 있게 한다 — 이게 컨테이너 환경의 "graceful"이다.

### 언제 gunicorn 이 필요하고, 언제 사족인가

| 배포 환경 | 수평 확장(코어/병렬)을 누가? | 죽은 프로세스 교체를 누가? | 무중단 재배포를 누가? | 권장 |
|---|---|---|---|---|
| **VM / 베어메탈 / 단일 큰 컨테이너** (직접 운영) | 내가 — **gunicorn `-w N`** | **gunicorn 마스터** | **gunicorn graceful reload** | **gunicorn + UvicornWorker** ✅ |
| **Cloud Run / 서버리스** (우리) | **플랫폼**(인스턴스 오토스케일) | **플랫폼**(인스턴스 교체) | **플랫폼**(리비전 롤아웃) | **uvicorn 단일 워커** + 오토스케일 ✅ |
| **Kubernetes** (Deployment/HPA) | **HPA**(Pod 오토스케일) | **kubelet**(liveness→Pod 재시작) | **롤링 업데이트** | 보통 **uvicorn 단일**(Pod당 1) — gunicorn 은 중복 |

핵심: **"프로세스 감독"을 이미 해주는 상위 오케스트레이터(Cloud Run·K8s)가 있으면, 그 안에서 gunicorn 을 또 쓰는 건 이중 관리**다. gunicorn 은 **내가 직접 프로세스 감독을 책임져야 하는 환경(VM·베어메탈)** 에서 진가를 발휘한다. 우리 통화 서버는 **I/O 대기 위주**(WebSocket·Gemini 스트림)라 인스턴스당 워커 1개의 async concurrency 만으로도 다수 통화를 감당하고(6장), 병렬·감독은 Cloud Run 에 맡긴다.

### 만약 우리가 VM 으로 옮긴다면

Cloud Run 을 떠나 한 대의 큰 VM(예: 4코어)에서 직접 운영한다면, 그때는 `gunicorn main:app -k uvicorn.workers.UvicornWorker -w 4` 가 **정답에 가까워진다** — 다만 워커 수 × (엔진 풀 + genai 클라이언트) 자원이 곱으로 늘고 **DB 커넥션이 워커 수배**가 되므로(7장 pgbouncer 유한 커넥션), `-w` 는 코어 수·커넥션 예산 안에서 잡아야 한다. "워커 남발 → 커넥션 고갈"은 6장과 같은 함정이다.

## 6. 흔한 오해 / 함정

- ❌ **"Gunicorn 만으로 FastAPI(async)를 돌릴 수 있다."** → 아니다. Gunicorn 기본 워커는 **동기(WSGI)** 다. ASGI 앱은 반드시 `-k uvicorn.workers.UvicornWorker`(또는 다른 ASGI 워커 클래스)를 줘야 한다. 안 그러면 async 앱이 제대로 안 돈다.
- ❌ **"Cloud Run 에서도 gunicorn 다중 워커를 쓰면 더 빠르다."** → 이중 스케일이라 대개 손해다. Cloud Run 은 **인스턴스**로 수평 확장하는데 그 안에서 또 워커를 N개 두면, 인스턴스당 메모리·DB 커넥션이 N배(6장 startup N회)가 돼 **커넥션 고갈·자원 회계 혼란**만 는다. 컨테이너당 워커 1 + 인스턴스 오토스케일이 정석.
- ❌ **"워커 수는 많을수록 좋다."** → 코어 수를 넘는 워커는 CPU 를 서로 뺏어 문맥전환만 늘린다(10장). 게다가 gunicorn `-w N` 은 자원 N배다. 통념: CPU 바운드 ≈ 코어 수, I/O 바운드는 그 근처.
- ⚠️ **`--timeout` 을 짧게 두면 장기 연결이 죽는다.** 동기 워커에서 `--timeout` 은 "워커가 그 시간 응답 없으면 kill". 스트리밍·WebSocket·느린 업로드가 있으면 정상 연결을 먹통으로 오판할 수 있다. async(UvicornWorker)에선 heartbeat 방식이 달라 대개 안전하지만, 값은 워크로드에 맞춰야 한다.
- ⚠️ **Gunicorn 은 Windows 에서 안 돈다.** 로컬이 Windows 면 개발은 `uvicorn --reload`, gunicorn 조합은 리눅스(컨테이너/CI/서버)에서만 검증된다.

## 7. 요약

- **Gunicorn = 프로세스 매니저(감독)**, **Uvicorn(UvicornWorker) = 실제 ASGI 실행**. 표준 조합은 `gunicorn main:app -k uvicorn.workers.UvicornWorker -w N`.
- Gunicorn 이 주는 것: 죽은 워커 **재시작**, `--max-requests` **워커 재활용**(메모리 누수 방어), **graceful restart**(무중단 재배포), `--timeout` 먹통 워커 kill.
- 개념(마스터-워커 감독 트리)은 `uvicorn --workers 2` 로도 재현된다 — (b)에서 **워커들의 ppid 가 하나의 마스터로 모이는 것**을 실측했다.
- **Gunicorn 은 Linux 전용**(Windows 미지원). 이 머신선 명령·config 만 보여주고 미실행.
- **우리 배포**: Dockerfile 은 gunicorn 없이 `uvicorn main:app` **단일 워커**. 병렬·감독·무중단 배포를 **Cloud Run 오토스케일**이 대신하기 때문. gunicorn 은 **VM/베어메탈처럼 내가 직접 프로세스를 감독해야 하는 환경**에서 필요하다.

## 8. 연습문제

1. `gunicorn main:app -w 4` 만 주고 `-k uvicorn.workers.UvicornWorker` 를 빠뜨리면 우리 FastAPI 앱은 왜 문제가 되나? (4장 ASGI/WSGI 로 설명)
2. `--max-requests 1000` 이 방어하려는 문제는 무엇이고, `--max-requests-jitter` 가 없으면 어떤 부작용이 생기나?
3. 우리 Dockerfile 이 gunicorn 없이 단일 uvicorn 워커로 가는 이유를, Cloud Run 오토스케일과 "프로세스 감독을 누가 하나" 관점에서 설명하라. 만약 한 대의 4코어 VM 으로 옮긴다면 명령은 어떻게 바뀌고 무엇을 조심해야 하나(7장 커넥션 연결)?

<details>
<summary>답</summary>

1. Gunicorn 기본 워커는 **동기(WSGI)** 라 async(ASGI) 앱의 이벤트 루프를 제대로 돌리지 못한다. FastAPI 는 ASGI 앱(4장)이므로, gunicorn 이 감독하는 워커 자리에 **ASGI 를 아는 UvicornWorker** 를 `-k` 로 앉혀야 async 코루틴/WebSocket 이 정상 동작한다.
2. `--max-requests` 는 **장기 실행 파이썬 프로세스의 미세한 메모리 누수/단편화**를 주기적 워커 재활용으로 리셋한다. jitter 가 없으면 모든 워커가 **거의 동시에** 1000요청에 도달해 **한꺼번에 재시작** → 그 순간 처리 용량이 확 떨어져 순단(latency spike)이 난다. jitter 는 재활용 시점을 워커마다 흩어 이를 막는다.
3. Cloud Run 이 **컨테이너 인스턴스를 오토스케일**하며 죽은 인스턴스 교체·리비전 롤아웃까지 하므로, gunicorn 의 "병렬 확장 + 프로세스 감독 + 무중단 배포" 역할을 **플랫폼이 인스턴스 단위로** 대신한다. 그래서 컨테이너 안에선 워커 1개면 충분(자원 회계·장애 격리 단순). 4코어 VM 으로 옮기면 `gunicorn main:app -k uvicorn.workers.UvicornWorker -w 4` 가 합리적이지만, 워커 4개면 **엔진 풀·genai 클라이언트·DB 커넥션이 4배**가 되니 pgbouncer 유한 커넥션(7장) 예산 안에서 `-w` 와 풀 크기를 잡아야 한다.

</details>

---

이전 → [11. Epoll — 이벤트 루프의 진짜 엔진](11-epoll.md)

다음 → [13. Multiprocessing — CPU 바운드를 진짜 병렬로](13-multiprocessing.md)

돌아가기 → [교과서 목차(README)](README.md)
