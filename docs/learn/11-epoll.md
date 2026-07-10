# 11. Epoll — 이벤트 루프의 진짜 엔진

> **한 줄 요약**: [2장](02-event-loop.md)의 이벤트 루프가 "한 스레드로 수천 연결"을 해내는 비결은 마법이 아니라 **OS 의 I/O 멀티플렉싱**이다. 소켓은 파일 디스크립터(fd)이고, OS 에 "이 fd 들 중 준비된 게 생기면 알려줘"라고 물어보는 시스템콜이 있다. 리눅스의 그 도구가 **epoll** — 준비된 fd 만 골라줘서(옛 `select` 는 매번 전부 스캔) 연결이 많아도 빠르다.

**이 챕터의 키워드**: Epoll(Linux), Kqueue(macOS), IOCP(Windows), File Descriptor, System Call, selectors

> 이전 챕터([10. Context Switching & CPU Cache](10-context-switching-cpu-cache.md))에서 "왜 스레드를 무작정 늘리면 안 되나 → async 로 문맥교환을 피한다"를 봤다. 그럼 그 async 이벤트 루프는 **밑에서 무슨 시스템콜로** 수천 소켓을 지켜보나? 이 장이 [2장](02-event-loop.md) 그림의 **빈칸**을 채운다.

---

## 1. 왜 중요한가

우리 서버의 WebSocket 통화는 연결 하나가 **5분간 열려 있다**. 동시 통화가 1000건이면 소켓 1000개가 동시에 살아 있다. 대부분의 순간 그 소켓들은 **아무 일도 안 한다**(사용자가 말하는 사이, 비버가 말하는 사이 조용하다). 그중 **지금 데이터가 도착한 소켓 몇 개만** 처리하면 된다.

문제는 "1000개 중 지금 준비된 몇 개"를 **어떻게 아느냐**다.
- 순진한 방법: 1000개를 하나씩 "너 데이터 왔어? 너는? 너는?" 물어본다(polling) → 대부분 "아니"라서 낭비. 연결이 늘수록 O(n).
- epoll 방식: OS 에 1000개를 **한 번 등록**해두고 "준비된 것들만 리스트로 줘"라고 한 번 물어본다 → 준비된 것만 돌려받음. 연결이 많아도 싸다.

이걸 모르면 "async 가 어떻게 스레드 없이 동시에 되지?"가 영영 마법으로 남는다. epoll 이 그 마법의 정체다.

## 2. 개념 — 비유로 시작

**비유 (식당 서빙)**: 테이블(연결) 1000개를 종업원 한 명(이벤트 루프 스레드)이 맡는다.
- **`select`/`poll` 방식(옛날)**: 종업원이 **매번 1000 테이블을 순회**하며 "부르셨어요?"를 다 확인한다. 999 테이블이 조용해도 전부 돈다 → 테이블이 늘수록 한 바퀴가 길어진다(O(n)).
- **`epoll` 방식(리눅스)**: 테이블마다 **호출벨**을 달아 프런트 데스크(커널)에 등록해둔다. 종업원은 데스크에 "벨 눌린 테이블 목록 줘"라고 **한 번** 묻고, 눌린 테이블만 간다. 조용한 999 테이블은 쳐다도 안 본다 → 테이블이 많아도 빠르다(준비된 수에만 비례).

**정확한 정의**:
- **File Descriptor(fd)**: 커널이 열린 자원(소켓·파일·파이프)에 붙이는 **정수 번호표**. 소켓 하나 = fd 하나. 유닉스 철학 "모든 것은 파일".
- **System Call(시스템콜)**: 유저공간 프로그램이 커널에 일을 부탁하는 경계 넘기. 소켓 `recv`/`send`, `epoll_wait` 등이 syscall 이다. 경계를 넘는 비용이 있어(모드 전환) **자주 부르면 비싸다** → 논블로킹 + 배칭으로 줄인다.
- **I/O 멀티플렉싱**: fd **여러 개**를 한 스레드가 동시에 지켜보다가 준비된 것만 처리하는 기법. `select` → `poll` → `epoll`(리눅스)/`kqueue`(BSD·macOS) 순으로 발전.
- **epoll**: 리눅스의 확장 가능한 I/O 멀티플렉서. fd 를 **한 번 등록**(`epoll_ctl`)해두고 `epoll_wait` 로 **준비된 fd 만** 받는다. 등록 목록을 커널이 들고 있어 매 호출마다 전체를 넘길 필요가 없다 → 연결 수 n 이 커도 스캔이 준비된 개수에만 비례(옛 `select` 의 O(n) 문제 해결).
- **selectors(파이썬 표준 모듈)**: 위 OS별 도구들을 **하나의 API 로 감싼** 표준 라이브러리. `DefaultSelector` 가 알아서 그 OS 최고를 고른다(리눅스=epoll, macOS=kqueue, Windows=select). **asyncio 도 내부적으로 이 위에서 돈다.**

## 3. 그림

```
[2장에서 남긴 빈칸] 이벤트 루프는 "준비된 것"을 어떻게 아나?
                                  ┌─────────────────────────┐
   asyncio 이벤트 루프  ──물어봄──▶│  selector.select()      │
   (uvicorn, 단일 스레드)          │  = OS I/O 멀티플렉싱     │
                      ◀─준비된 fd─│  (리눅스: epoll_wait)    │
                                  └─────────────────────────┘
                                     ▲ 커널이 fd 등록목록 보유

[select (옛날) vs epoll (리눅스)]  소켓 1000개, 지금 준비된 건 3개라 치자
  select:  유저→커널로 1000개 fd 통째 전달, 커널이 1000개 전부 검사 → O(n)
           [■■■■■■■■...(1000)...■■■]  매 호출마다 전부 스캔
  epoll :  epoll_ctl 로 1000개 '한 번' 등록해둠. epoll_wait 는 준비된 3개만 반환
           등록: (1회) → wait: [준비3개] ← 조용한 997개는 안 건드림 → 확장성 ↑

[한 스레드가 여러 fd 를 다중화]  (4절 미니 에코 서버가 이걸 실제로 함)
  루프: while 살아있음:
          ready = selector.select()      # 준비된 소켓들 한 번에
          for fd in ready:               # 준비된 것만 순회
              데이터 읽고 → 에코 → 다음 fd
        스레드는 1개. fd 는 여러 개. 문맥교환 없이 번갈아 처리.
```

## 4. 직접 돌려보자

> ⚠️ **정직 고지**: 지금 이 기기는 **Windows** 라 진짜 `epoll` 은 못 돌린다(epoll 은 리눅스 전용 syscall). 파이썬 `selectors` 모듈이 Windows 에선 자동으로 **select** 백엔드로 떨어진다. 아래 (a)로 이 사실을 눈으로 확인하고, (b)의 미니 서버 코드는 **한 줄도 안 바꾸고** 리눅스에 올리면 그 자리에서 epoll 로 돈다 — 우리 서버가 실제로 도는 **Cloud Run(리눅스)** 이 바로 그 경우다.

### (a) 이 OS 는 어떤 백엔드를 쓰나

파일: [`examples/11_selector_backend.py`](examples/11_selector_backend.py)

```bash
python examples/11_selector_backend.py
```

실제 출력(Windows):
```
platform             : win32
DefaultSelector 클래스: <class 'selectors.SelectSelector'>
실제 인스턴스 타입    : SelectSelector

이 파이썬 빌드에 존재하는 selector 백엔드:
  EpollSelector   : 없음(이 OS에 없음)
  KqueueSelector  : 없음(이 OS에 없음)
  DevpollSelector : 없음(이 OS에 없음)
  PollSelector    : 없음(이 OS에 없음)
  SelectSelector  : 있음

OS 별 기본 백엔드(개념):
  Linux   → EpollSelector   (epoll,  준비된 fd만 O(1)에 가깝게)
  macOS   → KqueueSelector  (kqueue, 준비된 이벤트만)
  Windows → SelectSelector  (select, 매번 전체 fd 스캔 O(n))
```

**어디를 보라**: Windows 파이썬 빌드엔 `EpollSelector` **클래스 자체가 존재하지 않는다**(리눅스 전용). 그래서 `DefaultSelector` 가 `SelectSelector` 로 떨어졌다. **같은 스크립트를 리눅스에서 돌리면** `DefaultSelector 클래스` 줄이 `EpollSelector` 로 찍힌다. 즉 우리 코드는 그대로인데 **밑의 엔진만 OS 따라 바뀐다** — 그게 `selectors`(와 asyncio)의 이식성이다.

### (b) 한 스레드가 여러 소켓을 동시에 — 미니 에코 서버

파일: [`examples/11_echo_server_selectors.py`](examples/11_echo_server_selectors.py)

**서버 스레드는 딱 하나.** 그 한 스레드가 `selector.select()` 로 "지금 읽을 게 준비된 소켓들"을 받아 차례로 에코한다. 클라 3개를 서로 다른 리듬으로 붙여, 한 루프가 여러 연결을 번갈아 처리하는 걸 로그로 본다.

```bash
python examples/11_echo_server_selectors.py
```

실제 출력:
```
[server] selector 백엔드 = SelectSelector
[main] 서버 준비됨 port=60194, 클라 3개 접속

[server] accept fd=356 (열린 연결 1개)
[server] accept fd=328 (열린 연결 2개)
[server] accept fd=320 (열린 연결 3개)
[server]   loop 처리: fd=320 에코 b'c0-m0'
[client 0] 보냄 b'c0-m0' → 받음 b'c0-m0'
[server]   loop 처리: fd=328 에코 b'c1-m0'
[client 1] 보냄 b'c1-m0' → 받음 b'c1-m0'
[server]   loop 처리: fd=320 에코 b'c0-m1'
[client 0] 보냄 b'c0-m1' → 받음 b'c0-m1'
[server]   loop 처리: fd=356 에코 b'c2-m0'
[client 2] 보냄 b'c2-m0' → 받음 b'c2-m0'
[server]   loop 처리: fd=320 에코 b'c0-m2'
[client 0] 보냄 b'c0-m2' → 받음 b'c0-m2'
[server] close  fd=320
[server]   loop 처리: fd=328 에코 b'c1-m1'
[client 1] 보냄 b'c1-m1' → 받음 b'c1-m1'
[server]   loop 처리: fd=356 에코 b'c2-m1'
[client 2] 보냄 b'c2-m1' → 받음 b'c2-m1'
[server]   loop 처리: fd=328 에코 b'c1-m2'
[client 1] 보냄 b'c1-m2' → 받음 b'c1-m2'
[server] close  fd=328
[server]   loop 처리: fd=356 에코 b'c2-m2'
[client 2] 보냄 b'c2-m2' → 받음 b'c2-m2'
[server] close  fd=356
[server] 종료. 한 스레드가 처리한 메시지 총 9건
```

**어디를 보라**: 서버 로그의 `fd=` 번호가 **320 → 328 → 320 → 356 → 320 → 328 …** 로 클라들 사이를 오간다. **스레드는 하나뿐인데** 세 연결(fd 320/328/356)을 번갈아 처리한다 — 문맥교환 없이([10장](10-context-switching-cpu-cache.md)), `select()` 가 "지금 준비된 소켓"을 그때그때 골라주기 때문이다. 마지막 줄: 한 스레드가 총 9건을 처리했다. **이게 이벤트 루프의 심장**이고, 연결이 3개가 아니라 3000개여도 원리는 같다(리눅스라면 epoll 이 골라준다).

> 핵심 구조 (예제에서 발췌):
> ```python
> events = sel.select(timeout=0.2)   # 준비된 fd 만 돌려준다
> for key, _mask in events:
>     if key.data is None:           # 리슨 소켓 → accept 로 새 연결 등록
>         ...
>     else:                          # 클라 소켓 → recv 해서 에코
>         ...
> ```
> `sel.select()` 한 줄이 리눅스에선 `epoll_wait` syscall 로, Windows 에선 `select` syscall 로 내려간다.

## 5. OS 별 멀티플렉싱 한눈에 (개념 표)

Windows 라 (b)는 select 로 돌았지만, 각 OS 의 진짜 엔진은 다르다:

| OS | 도구 | 모델 | 특징 |
|---|---|---|---|
| Linux | **epoll** | **준비 통지(readiness)**: "이 fd 읽을 수 있어" | fd 등록 1회 + 준비된 것만 반환, 대량 연결에 강함. asyncio 기본. |
| macOS/BSD | **kqueue** | 준비 통지 | epoll 과 비슷한 확장성, 이벤트 종류가 더 일반적(파일·시그널·타이머도). |
| Windows | **IOCP** | **완료 통지(completion)**: "이 읽기 작업이 *끝났어*" | 모델이 다름 — "준비됐다"가 아니라 "다 됐다"를 알림(Proactor). |
| (폴백) | select/poll | 준비 통지, O(n) | 이식성 최고지만 fd 많으면 느림. Windows selectors 기본. |

- **readiness vs completion**: epoll/kqueue 는 "이제 읽어도 안 막혀"(준비)를 알리면 **내가 read 한다**. IOCP 는 "읽기를 미리 걸어두면 커널이 다 해놓고 완료를 알린다". asyncio 는 리눅스에서 **SelectorEventLoop(epoll)**, Windows 에서 기본 **ProactorEventLoop(IOCP)** 를 쓴다 — 그래서 우리 코드는 같아도 OS 마다 밑엔진이 다르다.
- 실무 결론: **우리는 리눅스(Cloud Run)에 배포하므로 실제로는 epoll 이 통화 소켓 수천 개를 다중화한다.** Windows 로컬 개발은 select/IOCP 로 돌지만 애플리케이션 코드는 동일하다.

## 6. 우리 코드와 연결

- **[2장](02-event-loop.md) 빈칸 채우기**: 2장은 "이벤트 루프가 준비된 것만 골라 처리한다"까지 그렸다. **그 '고르는 일'의 정체가 이 장의 `selector.select()` → 리눅스 `epoll_wait`** 이다. uvicorn([5장](05-uvicorn.md))이 이벤트 루프를 돌리고, 그 루프가 매 틱마다 epoll 에게 "준비된 소켓 줘"를 물어 우리 WebSocket 핸들러를 깨운다.
- **WebSocket 통화 수천 동시 연결이 왜 가능한가**: 통화 소켓 대부분은 대부분의 순간 조용하다(발화 사이 침묵). epoll 은 **준비된 소켓만** 루프에 올리므로, 조용한 수천 연결이 있어도 루프는 지금 오디오가 도착한 몇 개만 처리한다. 스레드 수천 개(문맥교환 지옥, [10장](10-context-switching-cpu-cache.md))가 아니라 **fd 수천 개 + 한 루프 + epoll** 로 감당한다.
- **normalcall 2펌프도 이 위에**: 클라→Gemini, Gemini→클라 두 펌프([`call_session.py`](../../domains/learning/realtime/call_session.py))는 각각 소켓 `recv` 에서 `await` 한다. 그 `await` 가 결국 epoll 에 "이 fd 준비되면 깨워줘"로 등록되는 것이다. `asyncio.timeout` 절대 백스톱, `run_db` 의 스레드풀 오프로드([1장](01-gil.md))도 전부 이 단일 epoll 루프를 **막지 않으려는** 설계다.
- **syscall 을 아끼는 습관**: 오디오를 잘게 여러 번 `send` 하면 syscall(경계 넘기)이 그만큼 늘어 비싸다. 그래서 버퍼를 어느 정도 **모아서** 보내고(`bytearray` 누적), 논블로킹으로 처리한다 — 2절 "syscall 은 자주 부르면 비싸다"의 실무 반영.

## 7. 요약

- 이벤트 루프가 "한 스레드로 수천 연결"을 하는 진짜 엔진은 **OS I/O 멀티플렉싱**이다. 소켓 = **fd**, 지켜보는 syscall = 리눅스 **epoll**.
- 옛 `select`/`poll` 은 매 호출마다 전체 fd 를 스캔(O(n)). **epoll** 은 한 번 등록 + **준비된 fd 만** 반환해 대량 연결에 강하다.
- 파이썬 **`selectors`** 가 이걸 이식성 있게 감싼다: 리눅스=epoll, macOS=kqueue, Windows=select/IOCP. asyncio 도 그 위에 있다.
- Windows 에선 진짜 epoll 을 못 보지만((a)로 확인), 코드는 그대로 리눅스(Cloud Run)에서 epoll 로 돈다. (b) 미니 에코 서버가 "한 스레드·여러 fd 다중화"를 실제로 보여준다.
- 이것이 [2장](02-event-loop.md) 그림의 빈칸이자, 우리 WebSocket 통화 수천 동시 연결의 하드웨어적 토대다.

## 8. 연습문제

1. (b) 미니 에코 서버에서 서버 로그의 `fd=` 번호가 클라들 사이를 오가는 게 왜 "한 스레드로 동시 처리"의 증거인가? 만약 클라마다 스레드를 따로 뒀다면 로그가 어떻게 달랐을까?
2. `select` 가 O(n) 인데 `epoll` 이 대량 연결에서 빠른 이유를, "fd 등록을 누가 들고 있나"로 한 문장으로 설명하라.
3. asyncio 에서 `await websocket.receive_bytes()` 를 호출하면, 이 장의 어느 syscall 로 내려가고 그동안 이벤트 루프는 무엇을 하나? ([2·3장](02-event-loop.md)과 연결)
4. 우리 서버가 Windows 에서 돌 때와 Cloud Run(리눅스)에서 돌 때, `selectors.DefaultSelector` 는 각각 무엇을 고르나? 애플리케이션 코드는 바꿔야 하나?

<details>
<summary>답</summary>

1. 서버 스레드가 **하나뿐인데** fd 320/328/356 을 번갈아 처리한다는 건, 그 한 스레드가 `select()` 로 준비된 소켓을 그때그때 골라 다중화한다는 뜻이다. 클라마다 스레드를 따로 뒀다면 각 스레드가 자기 소켓만 붙잡고 블로킹 `recv` 했을 것이고, 로그의 "한 서버 루프가 여러 fd 를 오간다"는 패턴 대신 스레드별로 독립 처리됐을 것이다(대신 문맥교환·스레드 비용이 늘어남 — [10장](10-context-switching-cpu-cache.md)).
2. `select` 는 매 호출마다 유저가 **전체 fd 목록을 커널에 넘겨** 커널이 전부 검사하지만, `epoll` 은 **커널이 등록 목록을 계속 들고 있어서** `epoll_wait` 는 준비된 것만 돌려주면 된다 → 조용한 연결 수와 무관하게 준비된 개수에만 비례한다.
3. 리눅스라면 결국 **`epoll_wait`** (그리고 준비되면 소켓 `recv`) syscall 로 내려간다. `await` 하는 동안 그 코루틴은 멈추고, 이벤트 루프는 epoll 에 이 fd 를 등록해둔 채 **다른 준비된 코루틴들(다른 통화)을 계속 처리**한다. 소켓에 데이터가 도착해 epoll 이 알리면 루프가 이 코루틴을 다시 깨운다([3장](03-coroutine-asyncio.md) "멈췄다 이어지는 함수").
4. Windows: **SelectSelector**(진짜 epoll 없음, select 폴백). Cloud Run 리눅스: **EpollSelector**(진짜 epoll). **애플리케이션 코드는 바꿀 필요가 없다** — `selectors`/asyncio 가 OS별 최적 백엔드를 자동 선택하므로 이식성이 보장된다.

</details>

---

이전 → [10. Context Switching & CPU Cache — 밑바닥에서 무슨 일이](10-context-switching-cpu-cache.md)

다음 → [12. Gunicorn + Uvicorn — 프로세스 매니저](12-gunicorn-uvicorn.md)

돌아가기 → [교과서 목차(README)](README.md)
