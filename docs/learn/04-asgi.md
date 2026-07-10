# 04. ASGI vs WSGI — FastAPI가 서 있는 땅

> **한 줄 요약**: WSGI 는 "요청 1개 = 함수 1콜(동기·블로킹)" 규격이라 WebSocket·롱커넥션·async 를 못 다룬다. **ASGI 는 `async def app(scope, receive, send)` 라는 async 인터페이스 표준**이라 HTTP·WebSocket·수명주기를 async 로 다룬다. FastAPI 는 이 ASGI 를 구현한 **Starlette** 툴킷 위에 얹혀 있다.

**이 챕터의 키워드**: ASGI, WSGI, Starlette, Lifespan, Middleware, Dependency Injection, Background Task

---

## 1. 왜 중요한가

모듈 1 에서 "한 스레드 + 이벤트 루프로 수천 연결"을 배웠다. 그런데 그 async 세계와 **HTTP 요청/응답** 사이를 이어 주는 규격이 있어야 한다 — 그게 ASGI 다. 우리 서버의 핵심인 **normalcall 은 5분짜리 WebSocket** 이다. 옛 규격인 WSGI 로는 이걸 아예 못 만든다(WSGI 에 WebSocket 개념이 없다). "FastAPI 는 왜 async 가 되고, 왜 WebSocket 이 되고, 왜 `lifespan` 으로 시작·종료 훅을 거는가"의 답이 전부 이 한 글자 차이(**A**SGI)에 있다. 이 땅을 알아야 5장(Uvicorn)·6장(워커)이 무엇을 실행하는지 보인다.

## 2. 개념 — 비유로 시작

**비유**: 택배 회사에 두 가지 **접수 규격**이 있다.

- **WSGI** = "**한 명이 창구에 와서, 물건을 건네고, 그 자리에 서서 영수증을 받아 갈 때까지 창구가 닫힌 채로** 처리"하는 규격. 손님 1명 = 처리 1건 = 끝날 때까지 점유. 창구 직원(함수)은 물건을 받아 계산이 끝날 때까지 **다른 일을 못 한다(동기·블로킹)**. 그리고 이 규격에는 "택배 기사와 손님이 **계속 연결된 채 실시간으로 주고받는다**(WebSocket)"는 개념 자체가 없다.
- **ASGI** = "손님이 오면 **접수 이벤트를 큐에 넣고**, 직원은 준비된 접수만 async 로 처리"하는 규격. 한 연결에서 이벤트가 **여러 번** 오갈 수 있어(요청 시작 → 본문 여러 조각 → 응답 시작 → 응답 본문 여러 조각) HTTP 도, **양방향으로 계속 이어지는 WebSocket 도** 같은 방식으로 다룬다.

**정확한 정의**: ASGI 앱은 **호출 가능한 async 객체 하나**다.

```python
async def app(scope, receive, send): ...
```

- **`scope`**: 이 연결의 메타데이터 dict. `scope["type"]` 이 `"http"` / `"websocket"` / `"lifespan"` 중 하나다. 경로·메서드·헤더 등이 들어 있다.
- **`receive`**: `await receive()` 로 들어오는 이벤트를 하나씩 당겨오는 async 함수(요청 본문, WS 수신 메시지 등).
- **`send`**: `await send({...})` 로 나가는 이벤트를 내보내는 async 함수(응답 헤더, 응답 본문, WS 송신 메시지 등).

WSGI(`def app(environ, start_response)`)는 이 셋이 **동기**이고, 한 번의 호출로 요청→응답이 끝난다. ASGI 는 셋이 **async** 이고 이벤트를 여러 번 주고받아, **WebSocket·서버센트이벤트·수명주기**까지 하나의 규격으로 흡수한다.

> **Starlette** 은 이 raw ASGI 위에 라우팅·요청/응답 객체·미들웨어·WebSocket·BackgroundTask 같은 **편의 도구**를 얹은 경량 툴킷이다. **FastAPI 는 Starlette 을 상속**하고 그 위에 pydantic 검증·의존성 주입·OpenAPI 문서를 더한 것이다. 즉 우리가 매일 쓰는 `@app.get(...)`, `Depends`, WebSocket 라우터는 전부 이 ASGI 규격 위에서 돈다.

## 3. 그림

```
WSGI (동기, 요청 1개 = 함수 1콜):
  요청 ──> def app(environ, start_response) ──> [블로킹 처리] ──> 응답  (끝)
           └ 한 번 호출로 끝. WebSocket/수명주기 개념 없음. 처리 동안 점유.

ASGI (async, 이벤트를 여러 번 주고받음):
  scope = {"type":"http"|"websocket"|"lifespan", ...}   ← 이 연결이 뭔지
                     │
   async def app(scope, receive, send):
        event = await receive()   ← 들어오는 이벤트(요청본문/WS수신) 당김
        ...
        await send({...})         ← 나가는 이벤트(응답헤더/본문/WS송신) 밀어냄
                     │
        HTTP:  receive(요청본문) → send(응답시작) → send(응답본문)
        WS:    receive/send 를 연결이 살아있는 내내 여러 번 (양방향)
        수명:  type=="lifespan" 로 startup/shutdown 이벤트도 같은 규격

  FastAPI ─(상속)→ Starlette ─(구현)→ ASGI(scope/receive/send) ─(실행)→ Uvicorn(5장)
```

## 4. 직접 돌려보자

### (a) 원시 ASGI 앱 — 프레임워크 없이 손으로

파일: [`examples/04_raw_asgi.py`](examples/04_raw_asgi.py)

```python
async def app(scope, receive, send):
    print(scope["type"], scope.get("path"), scope.get("method"))
    event = await receive()                       # 들어오는 이벤트
    await send({"type": "http.response.start", "status": 200, "headers": [...]})
    await send({"type": "http.response.body", "body": "안녕, ASGI".encode()})
```

실행:
```bash
uv run --with httpx python examples/04_raw_asgi.py
```

실제 출력:
```
=== in-process 로 GET / 요청 ===
[scope]   type='http' path='/' method='GET'
[receive] 'http.request' body=b''
[send]    http.response.start (status=200)
[send]    http.response.body
--- 결과 ---
status=200
body='안녕, ASGI'
```

**어디를 보라**:
- **`[scope]`**: `type='http'`, `path='/'`, `method='GET'` — 이 연결이 무엇인지 담은 dict. 만약 WebSocket 이었다면 `type='websocket'` 이었을 것이다(우리 통화 WS 가 바로 그 경우). FastAPI 의 `Request` 객체는 이 scope 를 예쁘게 감싼 것에 불과하다.
- **`[receive]`**: `http.request` 이벤트를 `await receive()` 로 당겨왔다. 본문이 있으면 `body` 에 바이트로, 크면 여러 번 나눠 온다.
- **`[send]`**: 응답을 **두 이벤트로 나눠** 보냈다 — 먼저 `http.response.start`(상태·헤더), 다음 `http.response.body`(본문). 이 "여러 이벤트로 스트리밍"이 되기 때문에 ASGI 는 큰 파일·SSE·WebSocket 을 자연스럽게 다룬다.
- 그리고 이 전부가 **포트도 서버도 없이** `httpx.ASGITransport(app=app)` 로 앱 함수를 직접 호출해 돌았다. "ASGI 앱 = 그냥 async 함수 하나"라는 게 이렇게 눈에 보인다.

### (b) WSGI vs ASGI 대비표

| | **WSGI** | **ASGI** |
|---|---|---|
| 시그니처 | `def app(environ, start_response)` | `async def app(scope, receive, send)` |
| 동기/비동기 | 동기(블로킹) | 비동기(async/await) |
| 요청 처리 | 요청 1개 = 함수 1콜, 끝나면 종료 | 이벤트를 여러 번 receive/send |
| WebSocket | ❌ 불가(규격에 없음) | ✅ `scope["type"]=="websocket"` |
| 롱커넥션/SSE/스트리밍 | 어렵다 | 자연스럽다 |
| 수명주기(startup/shutdown) | 규격 밖(별도 훅) | ✅ `lifespan` 이벤트로 표준화 |
| 대표 서버 | Gunicorn(sync), uWSGI | **Uvicorn**, Hypercorn(5장) |
| 대표 프레임워크 | Flask, Django(전통) | **FastAPI/Starlette**, Django(async) |

> 핵심: 우리가 FastAPI 를 고른 순간 **WebSocket 통화·async DB 오프로드·lifespan 자원 준비**가 전부 가능해진 건, 밑바닥이 WSGI 가 아니라 ASGI 라서다.

### (c) Starlette 4대 요소가 '언제' 도는가 — Lifespan / Middleware / DI / BackgroundTask

파일: [`examples/04_starlette_features.py`](examples/04_starlette_features.py)

```python
@contextlib.asynccontextmanager
async def lifespan(app):            # 시작/종료 훅
    app.state.greeting = "안녕"; yield

@app.middleware("http")             # 요청을 감싸는 계층
async def logging_mw(request, call_next): ...

def get_greeting(request): ...      # Depends 대상(의존성)

@app.get("/hello")
async def hello(bg: BackgroundTasks, greeting: str = Depends(get_greeting)):
    bg.add_task(after_response, "비버")   # 응답 후 실행
```

실행:
```bash
uv run --with fastapi --with httpx python examples/04_starlette_features.py
```

실제 출력:
```
[lifespan] 시작: 공유 자원 준비 (엔진/세션팩토리 자리)

=== GET /hello ===
[middleware] --> 요청 진입 /hello
[DI] get_greeting 의존성 실행
[handler] hello 본문 실행
[middleware] <-- 응답 나감 status=200
[background] 응답 후 실행: 비버 인사 기록
[client] 받은 응답: {'message': '안녕, 비버'}

[lifespan] 종료: 자원 정리
```

**어디를 보라** — 실행 순서가 각 요소의 정체를 그대로 드러낸다:
1. **`[lifespan] 시작`** 이 요청보다 **먼저**, 딱 한 번. 앱이 뜰 때 공유 자원을 준비한다(우리 `main.py` 가 여기서 엔진·세션팩토리·genai 를 만든다).
2. 요청이 오면 **`[middleware] -->`** 가 핸들러를 **감싸며 먼저** 진입한다.
3. **`[DI] get_greeting`** 이 핸들러 본문 **직전**에 실행돼 인자(`greeting`)를 만들어 넣는다 — 라우터가 얇아지는 이유.
4. **`[handler]`** 본문 실행 → 응답 생성.
5. **`[middleware] <--`** 가 응답을 받아 나가며 감싸기를 닫는다(요청→응답을 양쪽에서 감싼다).
6. **`[background]`** 는 **응답이 나간 뒤**에 실행된다("응답은 빨리 주고, 무거운 뒷정리는 나중에").
7. 앱이 닫힐 때 **`[lifespan] 종료`** 로 자원 정리.

## 5. 우리 코드와 연결

- **Lifespan** → [`main.py`](../../main.py#L104) 의 `lifespan`. `yield` **이전**에 `build_engine` / `build_session_factory` / `_create_genai_client` 로 공유 자원을 만들어 `app.state` 에 담고, `yield` **이후**(`finally`)에 `engine.dispose()` 로 정리한다. (c)에서 본 "시작 먼저 한 번, 종료 나중에 한 번"이 실제로 이렇게 쓰인다. 전역 엔진을 두지 않고 `app.state.session_factory` 에 보관하는 것도 이 훅 덕분이다.
- **Middleware** → [`main.py`](../../main.py#L150) 의 `CORSMiddleware`. (c)의 `logging_mw` 처럼 모든 요청/응답을 **감싸는** 계층이라 CORS 헤더를 일괄로 붙인다. 그리고 [`http_exception_handler`](../../main.py#L121)(엄밀히는 exception handler)가 `HTTPException` 을 `{"detail":{"code","message"}}` 표준 바디로 바꾼다 — 규격화된 에러 응답도 이 계층의 일이다.
- **Dependency Injection** → [`core/deps.py`](../../core/deps.py#L34) 의 `get_current_member` / `get_db`. 라우터는 `member: CurrentMember`, `db: DbSession` 이라고 **선언만** 하면, (c)의 `[DI]` 처럼 FastAPI 가 핸들러 직전에 실행해 인자로 넣어 준다. 인증·세션 배선이 라우터 밖으로 빠져 라우터가 "DTO 검증 + service 호출"만 하는 얇은 층이 되는 이유(우리 아키텍처 규칙)가 이것이다.
- **Background Task(구분 주의)** → FastAPI 의 `BackgroundTasks`(응답 후 실행)와, 우리 통화의 [`call_session.py`](../../domains/learning/realtime/call_session.py#L249) 가 쓰는 `asyncio.create_task` 는 **다른 것**이다. 전자는 요청/응답 라이프사이클에 묶여 "응답 보낸 뒤" 돌지만, 우리 통화후 분석은 **WebSocket 세션과 독립적인 asyncio Task** 로 띄우고 `_analysis_tasks` set 에 강참조로 붙잡는다(3장 참고). 목적은 비슷("응답/통화를 막지 않고 뒤에서 처리")하지만 수단·수명은 다르니 혼동하지 말자. 우리 코드가 `BackgroundTasks` 대신 `create_task` 를 쓴 건, 분석 수명이 **한 HTTP 요청이 아니라 통화 세션**에 걸쳐 있기 때문이다.

## 6. 흔한 오해 / 함정

- ❌ **"FastAPI 가 곧 서버다."** → 아니다. FastAPI(=Starlette=ASGI 앱)는 **요청을 처리하는 함수**일 뿐, 소켓을 열고 연결을 받아 이 함수를 호출해 주는 **ASGI 서버(Uvicorn, 5장)** 가 따로 있다.
- ❌ **"WSGI 도 async 쓰면 되지 않나."** → WSGI 규격 자체가 동기 단발 호출이라 WebSocket·수명주기 이벤트를 표현할 방법이 없다. 그래서 새 규격(ASGI)이 나온 것.
- ❌ **"미들웨어는 요청 전에만 돈다."** → (c)처럼 `call_next` **앞뒤로** 돈다(요청·응답을 양쪽에서 감싼다). 응답 헤더 추가·타이밍 측정이 가능한 이유.
- ⚠️ **ASGITransport 는 lifespan 을 자동 실행하지 않는다.** 그래서 (c) 예제는 `app.router.lifespan_context(app)` 로 수동으로 감쌌다. 실제 운영에선 **Uvicorn 이** lifespan 이벤트를 보내 준다.
- ⚠️ **`BackgroundTasks`(FastAPI) ≠ `asyncio.create_task`.** 위 5절 참고. 전자는 요청 수명, 후자는 자유로운 태스크.

## 7. 요약

- **WSGI**: 동기·단발(요청1개=함수1콜), WebSocket/수명주기 없음. **ASGI**: `async def app(scope, receive, send)`, 이벤트를 여러 번 주고받아 HTTP+WebSocket+lifespan 을 async 로 흡수.
- **Starlette** = raw ASGI 위 툴킷, **FastAPI** = Starlette + 검증/DI/문서.
- **scope**(이 연결이 뭔지) / **receive**(들어오는 이벤트) / **send**(나가는 이벤트) 3종이 ASGI 의 전부.
- **Lifespan**(시작·종료 한 번) / **Middleware**(요청·응답 감싸기) / **DI**(핸들러 직전 주입) / **BackgroundTask**(응답 후) 는 각자 실행 시점이 다르다.
- 우리 코드: lifespan=자원 준비, CORS/에러핸들러=미들웨어층, `CurrentMember`/`DbSession`=DI, 통화후 분석=(BackgroundTasks 가 아닌) `create_task`.

## 8. 연습문제

1. `04_raw_asgi.py` 의 응답이 `http.response.start` 와 `http.response.body` **두 이벤트**로 나뉘는데, 만약 10MB 파일을 보낸다면 이 구조가 왜 유리할까?
2. WSGI 로는 우리 normalcall(5분 WebSocket)을 만들 수 없다. 그 이유를 이 챕터 용어로 한 문장으로 설명하라.
3. 우리 통화후 분석이 FastAPI `BackgroundTasks` 가 아니라 `asyncio.create_task` 로 떠 있는 이유를, "수명(lifecycle)"의 관점에서 설명하라.

<details>
<summary>답</summary>

1. `body` 이벤트를 **여러 번 나눠(스트리밍)** 보낼 수 있기 때문. 10MB 를 한 번에 메모리에 올려 보내지 않고, 조각마다 `http.response.body`(`more_body=True`)로 흘려보내면 메모리·지연이 준다. WebSocket·SSE 도 같은 "이벤트 여러 번" 구조 위에 선다.
2. WSGI 규격은 **동기 단발 호출**이라 연결을 살려 둔 채 양방향으로 이벤트를 주고받는 WebSocket(`scope["type"]=="websocket"`) 개념 자체가 없기 때문.
3. `BackgroundTasks` 는 **한 HTTP 요청의 응답 직후**에 묶여 실행되지만, 통화후 분석의 수명은 **WebSocket 통화 세션**에 걸쳐 있고 통화가 끝난 뒤에도 계속 돌아야 한다. 그래서 요청 수명에 종속되지 않는 독립 `asyncio.Task` 로 띄우고 `_analysis_tasks` 로 강참조를 유지한다(GC 방지, 3장).

</details>

---

다음 챕터 → [05. Uvicorn — 이벤트 루프를 서버로](05-uvicorn.md)
