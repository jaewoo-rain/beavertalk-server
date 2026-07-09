# 05. Uvicorn — 이벤트 루프를 서버로

> **한 줄 요약**: Uvicorn 은 4장의 **ASGI 앱을 실제로 실행하는 ASGI 서버**다. 이벤트 루프(uvloop/asyncio)를 돌리며 소켓을 `accept` 해 들어온 요청을 `scope/receive/send` 로 변환해 앱에 넘긴다. 이때 **동기 `def` 핸들러는 스레드풀로** 빼 주지만, **`async def` 안에서 블로킹하면 루프 전체가 멈춰** 모든 요청이 직렬화된다.

**이 챕터의 키워드**: Uvicorn, Hypercorn

---

## 1. 왜 중요한가

4장에서 "FastAPI 는 요청을 처리하는 **함수**일 뿐, 소켓을 여는 **서버**는 따로 있다"고 했다. 그 서버가 Uvicorn 이다. `uvicorn main:app` 한 줄이 우리 앱을 실제 트래픽에 연결한다. 그런데 여기엔 **성능을 통째로 좌우하는 함정**이 있다 — 모듈 1 에서 배운 "async 안 블로킹이 이벤트 루프를 막는다"가 **Uvicorn 위에서 그대로 재현**된다. 핸들러를 `def` 로 쓸지 `async def` 로 쓸지, 그리고 async 안에서 동기 DB/네트워크를 부르면 무슨 일이 나는지를 모르면, "FastAPI 는 빠르다는데 왜 요청이 줄줄이 밀리지?"의 늪에 빠진다. **이 장의 (a) 시연이 이 교과서 전체에서 가장 실무적인 순간이다.**

## 2. 개념 — 비유로 시작

**비유**: 4장의 "요리사(이벤트 루프)"와 "레시피(ASGI 앱)"를 떠올리자. Uvicorn 은 그 **주방 전체를 운영하는 매니저**다.
- 손님(소켓 연결)이 문으로 들어오면 매니저가 **자리를 받아(accept)**, 주문을 표준 양식(`scope/receive/send`)으로 적어 요리사에게 넘긴다.
- 요리사는 한 명(이벤트 루프 1개). 매니저는 이 요리사가 놀지 않게 준비된 주문만 계속 밀어 넣는다.
- **동기 `def` 요리**(오래 서서 지켜봐야 하는 요리)는 매니저가 **보조 주방(스레드풀)** 으로 빼서 시킨다 — 메인 요리사가 안 묶이게.
- 하지만 요리사에게 **"async 요리인데 중간에 5분간 멍때리는(블로킹)"** 주문을 주면, 그 5분간 **주방 전체가 정지**한다. 매니저도 어쩌지 못한다(비선점).

**정확한 정의**: Uvicorn 은 **ASGI 서버**다. 하는 일:
1. TCP 소켓을 열고 `accept` 로 연결을 받는다.
2. **이벤트 루프**를 돌린다 — 가능하면 `uvloop`(C 로 짠 빠른 루프, Linux/macOS), Windows 에선 표준 `asyncio`(+`SelectorEventLoop`/`ProactorEventLoop`).
3. HTTP/WebSocket 을 파싱해 **`scope/receive/send`** 로 만들어 ASGI 앱(FastAPI)을 호출한다.
4. 앱이 `send` 로 돌려준 이벤트를 HTTP/WS 프레임으로 직렬화해 소켓에 쓴다.

> FastAPI(Starlette)는 **동기 `def` 엔드포인트를 자동으로 `run_in_threadpool`**(AnyIO 워커 스레드)에 태운다. 그래서 동기 핸들러의 블로킹(동기 DB 등)은 루프를 막지 않는다. 반면 **`async def` 엔드포인트는 이벤트 루프에서 직접** 실행되므로, 그 안의 블로킹은 루프를 그대로 멈춘다. "무엇을 async 로 쓰느냐"가 아니라 "async 안에서 블로킹하지 않느냐"가 관건이다.

**Hypercorn(대안) 한 문단**: Hypercorn 도 ASGI 서버지만 **HTTP/2·HTTP/3(QUIC)** 와 Trio 이벤트 루프를 지원하는 게 차별점이다. Uvicorn 은 HTTP/1.1·WebSocket 에 집중해 가볍고 널리 쓰이며(사실상 표준), HTTP/2 가 필요하면 보통 **앞단 리버스 프록시**(nginx/Cloud Run 프런트엔드)가 담당하고 뒤의 Uvicorn 은 HTTP/1.1 로 둔다. 우리도 Uvicorn + Cloud Run 조합이다(6장). HTTP/2 를 앱 서버까지 내려야 하는 특수 상황이 아니면 Uvicorn 으로 충분하다.

## 3. 그림

```
             ┌──────────────────────── Uvicorn (ASGI 서버) ────────────────────────┐
  소켓 accept │  이벤트 루프(uvloop/asyncio) 한 개                                    │
  ───────────>│    파싱 → scope/receive/send  ──호출──>  FastAPI(ASGI 앱)            │
             │                                                                       │
             │   async def 핸들러 ─────────────> [이벤트 루프에서 직접 실행]          │
             │       await asyncio.sleep(..)  → 양보(OK)                              │
             │       time.sleep(..) / 동기DB   → ❌ 루프 정지 = 모든 요청 대기         │
             │                                                                       │
             │   def 핸들러 ────────────────> [스레드풀(run_in_threadpool)로 오프로드] │
             │       time.sleep(..) / 동기DB   → 스레드가 GIL 놓고 대기, 루프는 계속   │
             └───────────────────────────────────────────────────────────────────────┘

  => 같은 '1초 일'이라도 어디서 도느냐에 따라 서로 안 막기도(겹침), 통째로 막기도(직렬화) 한다.
```

## 4. 직접 돌려보자

### (a) ⭐ 동기 def / async 블로킹 / async 논블로킹 — 총 시간으로 증명

이 장의 하이라이트. 세 핸들러에 **각각 5개 요청을 동시에** 던지고 총 시간을 잰다.

파일: [`examples/05_sync_vs_async_block.py`](examples/05_sync_vs_async_block.py)

```python
@app.get("/sync-block")            # def + time.sleep → 스레드풀로 오프로드됨
def sync_block():        time.sleep(0.5); return {"h": "sync"}

@app.get("/async-block")           # async def + time.sleep → 루프를 막음(안티패턴)
async def async_block():  time.sleep(0.5); return {"h": "async-block"}

@app.get("/async-ok")              # async def + await → 논블로킹 양보
async def async_ok():   await asyncio.sleep(0.5); return {"h": "async-ok"}

# 각 경로에 gather 로 5개 동시 요청 → 총 시간 측정
```

실행:
```bash
uv run --with fastapi --with httpx python examples/05_sync_vs_async_block.py
```

실제 출력:
```
각 엔드포인트에 5개 요청 동시에 (각 0.5s 지연)

/async-ok     : 0.513 s
/sync-block   : 0.508 s
/async-block  : 2.506 s

참고: 완전히 겹치면 ~0.5s, 완전히 직렬이면 ~2.5s
```

**어디를 보라** — 세 값이 이 장의 결론 전부다:
- **`/async-ok` = 0.513s**: `await asyncio.sleep` 은 대기 중 루프에 양보하므로 5개 요청의 "0.5초 대기"가 **완전히 겹쳤다**(2장 gather 와 같은 원리). 이상적.
- **`/sync-block` = 0.508s**: `def` 핸들러라 FastAPI 가 **스레드풀로 오프로드**했다. 5개가 각자 스레드에서 자고(`time.sleep` 은 GIL 을 놓음, 1장) 겹쳐서 역시 ~0.5초. **동기 `def` 는 블로킹이어도 루프를 안 막는다** — FastAPI 가 대신 빼 줬기 때문.
- **`/async-block` = 2.506s**: `async def` 안에서 `time.sleep`(블로킹). 이건 **이벤트 루프에서 직접** 도는데 양보를 안 하니, 한 요청이 0.5초간 루프를 통째로 잡는다. 5개가 **줄서서 직렬** = 0.5×5 = **2.5초**. `gather` 로 동시에 던졌는데도 소용없다.

> **핵심 교훈**: `async def` 로 만든 게 빠른 게 아니다. **`async def` 안에서는 반드시 논블로킹(`await`)이거나, 블로킹이면 스레드풀로 오프로드**해야 빠르다. 확신이 없으면 차라리 `def` 로 두는 게 안전하다(FastAPI 가 알아서 빼 준다). 이 한 표(0.5 / 0.5 / 2.5)가 그 이유다.

**왜 이게 우리 서버에 치명적인가**: 우리 통화 핸들러는 `async def` WebSocket 이다. 그 안에서 실수로 동기 DB(`db.execute`)나 `requests.get` 을 직접 부르면, `/async-block` 처럼 **그 워커의 이벤트 루프가 멈춰 같은 워커의 모든 통화 오디오가 함께 끊긴다**. 그래서 우리는 동기 DB 를 `run_db`(스레드풀)로 오프로드한다(5절).

## 5. 우리 코드와 연결

- **`uvicorn main:app` 이 앱을 로드하는 법**: `main:app` = "`main.py` 모듈의 `app` 객체". [`main.py`](../../main.py#L244) 마지막 줄 `app = create_app()` 가 그 `app` 을 만든다(`create_app` 팩토리로 설정을 주입할 수 있게 해 두고, 모듈 전역에 `app` 을 노출해 `uvicorn main:app` 과 호환). Uvicorn 은 이 `app`(ASGI 콜러블)을 import 해 이벤트 루프에 올리고, lifespan 이벤트(4장)를 보내 [`lifespan`](../../main.py#L104) 의 startup 을 트리거한다 — 여기서 엔진·세션팩토리·genai 가 준비된다.
- **동기 `def` 의존성은 Uvicorn 밑에서 자동 오프로드**: [`core/deps.py`](../../core/deps.py#L34) 의 `get_current_member` 는 `async def` 가 아닌 **동기 `def`** 다. 그 안에서 `verify_token`·`find_or_create_by_auth`(동기 DB) 를 부르는데, (a)의 `/sync-block` 처럼 FastAPI 가 이걸 스레드풀에서 돌려 이벤트 루프를 막지 않는다. 라우터가 동기 DB 를 편하게 쓰면서도 서버가 안 굶는 이유.
- **`async def` 통화 안의 블로킹은 반드시 오프로드**: 통화 WebSocket 은 `async def` 라 루프에서 직접 돈다. 동기 SQLAlchemy 호출을 그대로 부르면 (a)의 `/async-block` 재현이다. 그래서 [`normalcall_service.py`](../../domains/learning/service/normalcall_service.py#L50) 의 `run_db` 가 `run_in_threadpool` 로 동기 쿼리를 스레드로 빼고 그 완료를 `await` 한다 — (a)의 `/sync-block` 과 같은 오프로드를 **우리가 손으로** 해 주는 셈(자동으로 안 빠지는 `async def` 문맥이라).
- **Windows vs Linux 루프**: 로컬(Windows)에선 표준 asyncio 루프, 배포(Linux 컨테이너)에선 Uvicorn 이 `uvloop` 를 쓸 수 있어 더 빠르다. 앱 코드는 그대로 — 어떤 루프든 우리는 "블로킹을 오프로드한다"는 원칙만 지키면 된다.

## 6. 흔한 오해 / 함정

- ❌ **"핸들러를 `async def` 로 바꾸면 빨라진다."** → (a)의 `/async-block`(2.5s)이 반례. async 안이 블로킹이면 오히려 **동기 `def` 보다 위험**하다(동기는 자동 오프로드, async 블로킹은 루프 직격).
- ❌ **"`gather`(동시 요청)면 알아서 겹친다."** → 각 요청의 핸들러가 양보해야 겹친다. `/async-block` 은 gather 로 던져도 2.5초(직렬).
- ❌ **"Uvicorn 이 곧 여러 코어를 쓴다."** → 기본은 **이벤트 루프 1개 = 프로세스 1개 = 코어 1개**. 코어를 다 쓰려면 워커(6장).
- ⚠️ **`--reload` 는 개발 전용**이다(파일 감시 프로세스가 붙어 오버헤드↑). 운영에선 끈다.
- ⚠️ **동기 `def` 핸들러도 스레드풀 크기 한계가 있다**(AnyIO 기본 40). 동기 블로킹 요청이 40개를 넘어 몰리면 대기가 생긴다 — 그래서 진짜 대기 많은 작업은 async+오프로드가 더 확장성이 좋다.

## 7. 요약

- Uvicorn = **ASGI 서버**: 소켓 accept → 이벤트 루프(uvloop/asyncio) → `scope/receive/send` 로 FastAPI 호출.
- `uvicorn main:app` = `main.py` 의 `app`(= `create_app()`)을 로드해 lifespan 을 트리거.
- **동기 `def` 핸들러/의존성** → FastAPI 가 **스레드풀로 자동 오프로드**(블로킹해도 루프 안 막음).
- **`async def` 핸들러** → 이벤트 루프에서 직접. 그 안의 블로킹은 **루프를 멈춰 전 요청 직렬화**((a) `/async-block` 2.5s). 반드시 논블로킹이거나 `run_db`/`to_thread` 로 오프로드.
- Hypercorn 은 HTTP/2·3 대안. 기본 웹서버는 Uvicorn, HTTP/2 는 앞단 프록시가 담당(우리 배포).

## 8. 연습문제

1. `05_sync_vs_async_block.py` 의 `N`(동시 요청 수)을 5 → 20 으로 늘리면 세 값은 각각 대략 어떻게 변할까?
2. `/async-block` 의 `time.sleep(0.5)` 를 `await asyncio.to_thread(time.sleep, 0.5)` 로 바꾸면 총 시간은? 왜?
3. 우리 통화(`async def` WS) 핸들러 안에서 `run_db` 없이 동기 쿼리를 직접 호출하면, Uvicorn 워커 하나에 붙은 다른 통화들에 무슨 일이 생길지 이 장의 (a) 결과로 설명하라.

<details>
<summary>답</summary>

1. `/async-ok` 는 여전히 **~0.5초**(20개 대기가 다 겹침). `/sync-block` 도 대략 **~0.5초**(20 ≤ 스레드풀 기본 40이라 다 겹침; 40 을 넘기면 늘기 시작). `/async-block` 은 **~10초**(0.5×20, 직렬 유지).
2. **~0.5초**로 떨어진다. `to_thread` 가 블로킹 `time.sleep` 을 스레드풀로 빼고 코루틴은 `await` 로 양보하므로, `/sync-block` 처럼 20…5개가 겹친다. = 우리 `run_db` 가 하는 오프로드의 축소판.
3. `/async-block`(2.5s) 재현. 동기 쿼리가 끝날 때까지 그 **워커의 이벤트 루프가 멈춰**, 같은 워커가 처리하던 **다른 통화들의 오디오 펌프·시계·flush 가 전부 정지**해 오디오가 끊기고 응답이 밀린다. 해결은 `run_db`/`to_thread` 오프로드.

</details>

---

다음 챕터 → [06. Worker Process — 코어를 다 쓰려면 프로세스를 늘려라](06-worker-process.md)
