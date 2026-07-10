# 03. Coroutine / async·await / asyncio — 멈췄다 이어지는 함수

> **한 줄 요약**: 코루틴은 "중간에 멈췄다가(`await`) 나중에 이어서 실행되는 함수"다. `asyncio` 는 이 코루틴들을 `Task`(=`Future`) 로 감싸 이벤트 루프에 올려 동시에 돌린다. `TaskGroup` 은 여럿을 **구조적으로 묶어** 하나 실패 시 형제를 취소한다.

**이 챕터의 키워드**: Coroutine, async, await, Future, Task, asyncio

---

## 1. 왜 중요한가

2장에서 "이벤트 루프가 준비된 작업을 골라 돌린다"고 했는데, 그 **"작업"의 정체가 코루틴/Task** 다. 우리 통화 본체는 코루틴 여러 개를 `TaskGroup` 으로 묶고, 통화후 분석은 `create_task` 로 백그라운드에 띄운다. 이 도구들의 정확한 의미 — "코루틴을 그냥 호출하면 안 돈다", "create_task 는 즉시 스케줄한다", "TaskGroup 은 하나 죽으면 형제를 취소한다" — 를 모르면 태스크가 소리 없이 사라지거나(GC), 예외가 삼켜지거나, 취소가 새는 버그를 만든다.

## 2. 개념 — 비유로 시작

**비유**: 코루틴은 **책갈피가 꽂히는 레시피**다. 보통 함수는 시작하면 끝까지 한 호흡에 실행된다. 코루틴은 `await`(양보 지점)에 오면 **책갈피를 꽂고 덮어 둔 뒤** 다른 레시피로 갔다가, 나중에 그 책갈피부터 이어서 요리한다.

- **코루틴 객체 vs 실행**: 레시피 책을 **펼치기만 한 것(코루틴 객체)** 과 **실제로 요리하는 것(await/run)** 은 다르다. `f()` 는 책을 펴서 건네줄 뿐, 아직 아무것도 안 익었다.
- **Future** = "**나중에 나올 요리의 영수증**". 지금은 없지만 완성되면 결과가 담긴다.
- **Task** = "요리사(루프)에게 **지금 이 레시피 맡아서 진행해**"라고 넘긴 것. 코루틴을 감싼 Future 이며, 넘기는 순간부터 백그라운드로 익는다.

**정확한 정의**:
- **코루틴 함수**: `async def` 로 정의한 함수. **호출하면 몸통이 실행되지 않고** '코루틴 객체'를 돌려준다.
- **`await x`**: `x`(코루틴/Future/Task)가 끝날 때까지 **현재 코루틴을 멈추고 루프에 양보**한다. 끝나면 그 자리에서 이어지며 결과를 받는다.
- **`Future`**: "아직 없는 결과의 약속." 루프가 완료를 표시하면 대기하던 코루틴이 깨어난다.
- **`Task`**: 코루틴을 감싸 **이벤트 루프에 즉시 등록**한 Future. 즉 "스케줄된 코루틴".

## 3. 그림

```
async def f(): ...        # 코루틴 '함수'
c = f()                   # 호출 => 코루틴 '객체' (아직 안 돎!)  [레시피 펼침]
                          #   await c  또는  asyncio.run(c)  해야 실행

asyncio.create_task(f())  # 코루틴 -> Task(Future) 로 감싸 즉시 루프에 등록
                          #   [요리사에게 맡김 => 백그라운드로 익기 시작]

await task                # 이 Task 의 결과(영수증)가 나올 때까지 양보하고 기다림

TaskGroup:                # 여러 Task 를 한 블록으로 묶음(구조적 동시성)
  async with TaskGroup() as tg:
     tg.create_task(A)    ┐
     tg.create_task(B)    ├─ 블록을 나갈 때 전부 완료를 보장
     tg.create_task(C)    ┘   하나라도 예외 => 형제 취소 + ExceptionGroup 로 모아 올림
```

## 4. 직접 돌려보자

### (a) 코루틴 호출은 즉시 실행이 아니다

파일: [`examples/03_coroutine_basics.py`](examples/03_coroutine_basics.py)

```python
coro = greet("비버")   # 아직 실행 안 됨 — 코루틴 객체
result = await coro    # 여기서 비로소 몸통 실행
# ... 그리고 await 를 잊은 코루틴은?
forgotten = greet("잊힌사람"); del forgotten  # GC 시 경고
```

실행:
```bash
python examples/03_coroutine_basics.py
```

실제 출력:
```
C:\...\examples\03_coroutine_basics.py:32: RuntimeWarning: coroutine 'greet' was never awaited
  del forgotten
RuntimeWarning: Enable tracemalloc to get the object allocation traceback
greet('비버') 반환 타입: coroutine
아직 몸통은 안 돌았다. 이제 await 한다 ->
await 결과: 안녕, 비버
(위/아래에 'coroutine ... was never awaited' 경고가 보이면 정상)
```

**어디를 보라**:
- `greet('비버') 반환 타입: coroutine` — 함수를 "호출"했는데도 반환은 **결과가 아니라 코루틴 객체**다. `await` 하기 전엔 `안녕, 비버` 가 안 나온다.
- 맨 위의 `RuntimeWarning: coroutine 'greet' was never awaited` — `await`(또는 `create_task`/`run`) 없이 버린 코루틴을 파이썬이 경고한다. (경고가 stderr 로 먼저 출력돼 순서가 위로 올라왔을 뿐, 이 줄이 핵심이다.) **"코루틴을 만들었으면 반드시 소비하라"** 는 신호다.

### (b) create_task 로 즉시 스케줄 → 겹쳐 실행, gather 로 수집

파일: [`examples/03_create_task_gather.py`](examples/03_create_task_gather.py)

```python
t1 = asyncio.create_task(work("A", 1.0))  # 즉시 스케줄(백그라운드 시작)
t2 = asyncio.create_task(work("B", 2.0))
results = await asyncio.gather(t1, t2)     # 완료를 기다려 결과만 수집
```

실행:
```bash
python examples/03_create_task_gather.py
```

실제 출력:
```
두 태스크 생성 직후 (아직 gather 안 함) — 이미 백그라운드로 도는 중
  [23s] A 시작
  [23s] B 시작
  [24s] A 끝 (1.0s)
  [25s] B 끝 (2.0s)
gather 결과: ['A', 'B']
총 시간: 2.015 s   <- max(1,2)=2초, 합(3초) 아님
```

**어디를 보라**: `A 시작`과 `B 시작`이 **같은 초([23s])** 에 찍혔다 — `create_task` 가 둘 다 즉시 루프에 올려 동시에 시작됐다는 증거. 총 시간은 `1+2=3초`가 아니라 **`max(1,2)=2초`**(둘이 겹침). `create_task` 는 "지금 스케줄해서 백그라운드로 돌려", `gather` 는 "이제 결과들 모아 줘"의 역할 분담이다. (참고: `await work(...)` 를 그냥 이어 썼다면 하나 끝나야 다음이 시작돼 3초가 됐을 것.)

### (c) TaskGroup — 하나 실패 시 형제 취소 + ExceptionGroup

파일: [`examples/03_taskgroup.py`](examples/03_taskgroup.py)

```python
async with asyncio.TaskGroup() as tg:
    tg.create_task(pump("펌프1", 0.3))
    tg.create_task(pump("펌프2", 0.3))
    tg.create_task(failing(1.0))     # 1초 뒤 ValueError
except* ValueError as eg:            # ExceptionGroup 을 종류별로 언패킹
    ...
```

실행:
```bash
python examples/03_taskgroup.py
```

실제 출력:
```
  펌프1 tick
  펌프2 tick
  펌프1 tick
  펌프2 tick
  펌프1 tick
  펌프2 tick
  failing: 이제 예외를 던진다
  펌프2 <- 형제 실패로 취소됨(정리 실행 가능)
  펌프1 <- 형제 실패로 취소됨(정리 실행 가능)
except* ValueError 로 잡음: ['펌프 하나가 죽음']
TaskGroup 블록을 벗어남 — 형제는 모두 정리되어 남는 태스크 없음
```

**어디를 보라**:
- 두 펌프가 한동안 `tick` 을 반복하다가, `failing` 이 예외를 던진 **순간** `펌프2`·`펌프1` 이 **자동 취소**된다. 내가 취소를 수동으로 호출한 적이 없다 — **TaskGroup 이 형제를 대신 취소**했다.
- 취소된 펌프는 `except asyncio.CancelledError` 에서 정리 로그를 찍고 `raise` 로 취소를 다시 올린다(정리만 하고 삼키지 않는 게 관례).
- 최종 예외는 낱개가 아니라 **`ExceptionGroup`** 으로 묶여 올라오고, `except* ValueError` 가 그 그룹에서 `ValueError` 만 골라 뽑는다. "여러 태스크가 동시에 죽을 수 있으니 예외도 묶음으로 다룬다"는 3.11 의 구조적 동시성 모델이다.

## 5. 우리 코드와 연결

- **`_run_session` = 바로 이 TaskGroup 패턴**: [`call_session.py`](../../domains/learning/realtime/call_session.py#L311) 는 (c)와 똑같이 `async with asyncio.TaskGroup() as tg` 안에서 **클라→Gemini 펌프 / Gemini→클라 펌프 / 시계 워처 / 점진 flush** 4개를 `tg.create_task` 로 띄운다. 한 펌프에서 클라가 끊기거나(`_ClientDisconnect`) 통화가 끝나면(`_CallFinished`) TaskGroup 이 **나머지 형제를 자동 취소**하고, 예외는 [`except* _CallFinished` / `except* _ClientDisconnect`](../../domains/learning/realtime/call_session.py#L319) 로 종류별로 받는다. (c)에서 본 `except*` 가 실전에서 이렇게 쓰인다.
- **절대 백스톱 = `asyncio.timeout`**: [`call_session.py`](../../domains/learning/realtime/call_session.py#L211) 의 `async with asyncio.timeout(ABSOLUTE_CALL_TIMEOUT_S)` 는 10분이 지나면 안쪽 전체를 `TimeoutError` 로 취소하는 상위 안전장치다. 코루틴을 밖에서 강제로 끝내는 취소가 어떻게 전파되는지(안쪽 await 지점에서 `CancelledError` 발생)를 (c)의 취소와 같은 원리로 이해하면 된다.
- **`asyncio.Event` = 이벤트로 깨우기**: [`call_session.py`](../../domains/learning/realtime/call_session.py#L99) 의 `playback_done_event = asyncio.Event()`. 클라가 재생 완료를 알리면 `set()` 하고([call_session.py:404](../../domains/learning/realtime/call_session.py#L403)), 종료 절차는 `await asyncio.wait_for(event.wait(), timeout=...)` 로 그 신호(또는 타임아웃)를 기다린다([call_session.py:530](../../domains/learning/realtime/call_session.py#L529)). "아직 없는 사건을 await 로 기다리다 준비되면 깨어난다"는 Future 의 감각 그대로다.
- **`create_task` + GC 방지 강참조**: (a)에서 "코루틴을 소비 안 하면 경고"를 봤다. Task 도 비슷하게, **어디서도 참조를 안 붙잡으면 refcount 가 0 이 돼(1장) 실행 도중 GC 될 수 있다.** 그래서 통화후 분석은 [`call_session.py`](../../domains/learning/realtime/call_session.py#L249) 에서 `create_task` 로 띄운 뒤 결과를 [`_analysis_tasks` set](../../domains/learning/realtime/call_session.py#L60) 에 넣어 강참조를 유지하고, 끝나면 `done_callback` 으로 제거한다([call_session.py:256-257](../../domains/learning/realtime/call_session.py#L256)). `create_task` 의 "즉시 스케줄"과 refcount 를 둘 다 알아야 이 관용구가 이해된다.
- **블로킹은 `to_thread`/executor 로**: 동기 DB 는 코루틴이 될 수 없다. [`normalcall_service.py`](../../domains/learning/service/normalcall_service.py#L50) 의 `run_db` 가 `run_in_threadpool`(내부적으로 executor)로 동기 함수를 별도 스레드에 실행하고 그 완료를 `await` 한다 — 블로킹 코드를 코루틴 세계에 **다리 놓는** 표준 방법이다(2장 오프로드와 동일).

## 6. 흔한 오해 / 함정

- ❌ **"`f()` 를 호출하면 실행된다."** → 아니다. 코루틴 객체만 생긴다. `await`/`create_task`/`asyncio.run` 이 있어야 돈다((a) 참고).
- ❌ **"`create_task` 결과는 안 붙잡아도 된다."** → 붙잡지 않으면 GC 로 사라질 수 있다("Task was destroyed but it is pending" 경고). 우리처럼 set 에 보관하거나 반드시 `await`.
- ❌ **`except Exception` 으로 TaskGroup 예외를 잡는다.** → TaskGroup 은 `ExceptionGroup` 을 던진다. 종류별로 잡으려면 **`except*`** 를 써야 한다((c)).
- ⚠️ **`CancelledError` 를 삼키지 마라.** 정리(`finally`)만 하고 다시 `raise` 하는 게 원칙. 삼키면 취소가 "새서" 상위가 태스크가 끝난 줄 안다((c)의 펌프가 `raise` 하는 이유).
- ⚠️ **`gather(..., return_exceptions=False)`(기본)** 는 하나가 실패해도 **다른 것을 취소하지 않는다**(예외만 올림). "하나 죽으면 형제 취소"가 필요하면 **TaskGroup** 을 써라 — 그래서 우리 통화 본체가 gather 가 아니라 TaskGroup 인 것.

## 7. 요약

- 코루틴 함수는 호출해도 안 돈다 → **코루틴 객체**. `await`/`run`/`create_task` 로 소비해야 실행.
- **Future** = 미래 결과의 약속. **Task** = 코루틴을 감싸 **즉시 스케줄**한 Future.
- `create_task` = 백그라운드 시작, `gather` = 결과 수집. 붙잡지 않은 Task 는 **GC 위험** → 강참조 보관.
- **`TaskGroup`(3.11+)** = 구조적 동시성: 블록 종료 시 전원 완료 보장, **하나 실패 → 형제 취소 + `ExceptionGroup`**. `except*` 로 언패킹.
- 블로킹 코드는 `to_thread`/executor(`run_db`)로 오프로드해 코루틴 세계와 연결.

## 8. 연습문제

1. `03_coroutine_basics.py` 에서 `del forgotten` 을 지우고 `await forgotten` 을 추가하면 경고는 어떻게 될까? 왜?
2. `03_create_task_gather.py` 에서 `create_task` 대신 `results = [await work("A",1.0), await work("B",2.0)]` 로 바꾸면 총 시간은? 왜?
3. 우리 `_run_session` 이 `asyncio.gather` 가 아니라 `TaskGroup` 을 쓰는 이유를, "펌프 하나가 예외로 죽었을 때 필요한 동작"의 관점에서 설명하라.

<details>
<summary>답</summary>

1. 경고가 **사라진다**. 코루틴을 `await` 로 소비했기 때문. `RuntimeWarning: never awaited` 는 "만들고 안 쓴" 코루틴에만 뜬다.
2. **3초**가 된다(`1 + 2`). `await` 를 순서대로 쓰면 A 가 끝나야 B 가 시작되므로 겹치지 않는다. `create_task` 로 미리 둘 다 스케줄해야 2초로 겹친다.
3. 통화에선 한 펌프가 죽으면(클라 끊김/서버 종료) **나머지 펌프·시계·flush 를 즉시 멈추고 정리**해야 한다. `gather`(기본)는 하나 실패 시 형제를 취소하지 않아 다른 펌프가 계속 돌며 자원을 잡는다. `TaskGroup` 은 하나 예외 시 **형제를 자동 취소**하고 예외를 `ExceptionGroup` 으로 모아 올려, `except* _CallFinished`/`except* _ClientDisconnect` 로 종료 사유별로 깔끔히 처리할 수 있다.

</details>

---

🎉 **모듈 1 완료!** 이제 파이썬이 "왜 빠르고 왜 느린지"의 밑바닥(GIL → 이벤트 루프 → 코루틴/asyncio)을 갖췄다.

다음 모듈 → [**04. ASGI vs WSGI — FastAPI가 서 있는 땅**](04-asgi.md)

돌아가기 → [교과서 목차(README)](README.md)
