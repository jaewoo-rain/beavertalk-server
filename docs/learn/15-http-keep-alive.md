# 15. HTTP Keep-Alive — 연결을 아껴 써라

> **한 줄 요약**: HTTP 요청마다 새 TCP 연결(3-way handshake)과 TLS 협상을 하면 **왕복 지연이 매번** 붙는다. **Keep-Alive** 는 한 TCP 연결을 닫지 않고 **여러 요청이 재사용**하는 것 — 7장 "커넥션 풀"의 HTTP 버전이다. 클라이언트(httpx)도 서버 사이 연결을 풀링해야 이득을 본다. HTTP/1.1(연결당 순차) → HTTP/2(한 연결 멀티플렉싱) → HTTP/3(QUIC, TCP HOL 제거)로 갈수록 "연결을 아끼는" 기술이 촘촘해진다.

**이 챕터의 키워드**: Keep-Alive, HTTP/1.1·2·3, TCP, TLS, Connection Pool(재등장), Reverse Proxy, Load Balancer

---

## 1. 왜 중요한가

우리 서버는 통화를 저장·분석하면서 외부 HTTPS 서비스를 쉴 새 없이 친다 — Supabase Storage 업로드, SpeechSuper 발음평가, Resend 이메일, 소셜/토큰 검증. 이 호출들이 매번 **새 TCP 연결 + TLS 핸드셰이크**를 하면, 실제 요청/응답(수 ms)보다 **연결 수립(왕복 여러 번, 수십 ms)** 이 훨씬 비싸 앱 전체가 느려진다. 7장에서 DB 연결을 풀로 재사용했듯, HTTP 연결도 재사용해야 한다.

방향이 두 개다. **우리가 외부로 나가는 쪽**(클라이언트로서): `httpx.Client` 를 재사용하면 keep-alive 로 연결을 아낀다 — 매 호출 새 클라이언트를 만들면 이득이 통째로 사라진다(5절에 실제 코드). **우리로 들어오는 쪽**(서버로서): Cloud Run 앞단 리버스 프록시/로드밸런서가 클라와 keep-alive 를 맺고, 그 뒤 인스턴스로 요청을 분배한다. 그리고 통화 WebSocket 은 아예 5분+ 연결을 **계속 유지**하는 극단적 keep-alive다. 이 장은 "연결은 비싸다, 그러니 닫지 말고 재사용하라"를 로컬에서 눈으로 확인한다.

## 2. 개념 — 비유로 시작

**비유**: 은행 창구에 갈 때마다 **입구에서 신분증 검사 + 소지품 검색 + 방문증 발급**을 새로 받아야 한다고 하자(= TCP + TLS 핸드셰이크). 볼일이 10초인데 입장 절차가 2분이면 미친 짓이다. **Keep-Alive** 는 한 번 들어간 뒤 방문증을 반납하지 않고, 다음 볼일도 **그 방문증으로 계속** 처리하는 것이다. 나갈 때(연결 종료)까지 재검색이 없다.

- **입장 절차 = 연결 수립(핸드셰이크)**. 처음 한 번만 비싸다.
- **방문증 = 열린 TCP 연결**. 반납(close) 전까지 다음 요청이 재사용.
- **방문증 유효시간 = keep-alive timeout**. 너무 짧으면 금방 만료돼 재입장(재핸드셰이크), 너무 길면 안 쓰는 사람이 창구(연결)를 계속 점유.
- **창구가 하나뿐 = HTTP/1.1** 한 연결에 한 볼일씩 순차. 동시에 여러 볼일을 보려면 **창구를 여러 개**(연결 여러 개) 연다.
- **한 창구에서 여러 볼일 병행 = HTTP/2** 멀티플렉싱. 한 방문증(연결)으로 여러 요청을 동시에 처리.

**정확한 정의**: Keep-Alive(HTTP persistent connection)는 응답을 보낸 뒤에도 TCP 연결을 **닫지 않고 열어 둬** 다음 HTTP 요청이 같은 연결을 쓰게 하는 것이다. HTTP/1.1 은 **기본이 keep-alive**(끄려면 `Connection: close`), HTTP/1.0 은 기본이 close 였다. TLS(https)에서는 이득이 특히 큰데, 연결마다 붙는 **TLS 핸드셰이크**(인증서 교환·키 합의, 왕복 1~2회)를 재사용으로 건너뛰기 때문이다. 이건 7장의 커넥션 풀과 **같은 아이디어의 네트워크 판**이다 — 다만 여기선 클라이언트가 "서버로 가는 TCP 연결"을 풀링한다.

> **7장과의 대칭**: 7장은 DB 연결을 `QueuePool` 로 재사용해 핸드셰이크를 없앴다(592ms → 7.8ms). 이 장은 HTTP 연결을 keep-alive 로 재사용해 TLS 핸드셰이크를 없앤다(아래 (a): 2995ms → 135ms). 다른 프로토콜, 같은 교훈: **연결은 비싸니 열어 두고 재사용하라.**

## 3. 그림

```
[keep-alive 없음] 요청마다 은행 재입장 (TCP + TLS 핸드셰이크)
  요청1: (TCP SYN/ACK)(TLS 협상 ~왕복)─[GET 응답]─(FIN 닫기)
  요청2: (TCP SYN/ACK)(TLS 협상 ~왕복)─[GET 응답]─(FIN 닫기)
  요청3: (TCP SYN/ACK)(TLS 협상 ~왕복)─[GET 응답]─(FIN 닫기)
  => 200요청 = 핸드셰이크 200번 (아래 (a): ~2995ms)

[keep-alive 있음 / HTTP/1.1] 한 연결을 순차 재사용
  준비:  (TCP+TLS 한 번) → 연결 열어 둠
  요청1: [GET 응답]  요청2: [GET 응답]  요청3: [GET 응답]  ...
  => 200요청 = 핸드셰이크 1번 (아래 (a): ~135ms). 단 '한 번에 하나씩'

[HTTP/2] 한 연결에 여러 요청을 스트림으로 동시에 (멀티플렉싱)
  연결 하나: │stream1 ═══│  │stream3 ══│
             │stream2 ════════│  │stream4 ═│   <- 동시에 흐름
  => 동시 50요청도 연결 1개 (아래 (c): TLS 핸드셰이크 1번)

[HTTP/1.1 로 동시 50요청 하려면] 연결을 50개 연다
  연결1[req] 연결2[req] ... 연결50[req]   <- 각자 TLS 핸드셰이크
  => 연결(=핸드셰이크) 50번 (아래 (c): HTTP/2보다 느림)
```

## 4. 직접 돌려보자

> ⚠️ 프로덕션/원격 서버에는 **일절 붙지 않는다.** 아래는 전부 로컬(127.0.0.1)에 잠깐 띄운 작은 FastAPI 앱이 대상이다. 평문 HTTP 의 로컬 TCP 핸드셰이크는 너무 싸서 차이가 안 보이므로, 서버를 **자체서명 인증서 HTTPS** 로 띄워 "새 연결마다 TLS 핸드셰이크"라는 진짜 왕복 비용을 재현했다(원격 https 를 정직하게 흉내 낸 것). `httpx.ASGITransport`(in-process)는 TCP 가 없어 이 데모엔 부적합해 **진짜 로컬 포트**로 띄운다.

### (a) ⭐ Client 재사용(keep-alive) vs 매번 새 Client — 실측

파일: [`examples/15_keepalive_reuse.py`](examples/15_keepalive_reuse.py)

```python
N = 200
# (1) 매 요청 새 Client → with 블록마다 연결을 닫으므로 매번 TLS 핸드셰이크
for _ in range(N):
    with httpx.Client(verify=cert) as client:
        client.get(f"{BASE}/ping")

# (2) Client 하나 재사용 → 첫 요청만 핸드셰이크, 나머지는 keep-alive 재사용
with httpx.Client(verify=cert) as client:
    for _ in range(N):
        client.get(f"{BASE}/ping")
```

실행:
```bash
uv run --with fastapi --with uvicorn --with httpx --with cryptography python 15_keepalive_reuse.py
```

실제 출력:
```
작업: GET /ping 를 200회 순차. 로컬 HTTPS(자체서명) → 새 연결마다 TLS 핸드셰이크

             매 요청 새 Client (재사용 X) :   2994.8 ms   (TLS 핸드셰이크 200회)
        Client 하나 재사용 (keep-alive) :    135.0 ms   (TLS 핸드셰이크 1회)

재사용이 약 22.2배 빠름 — 차이는 순수하게 '연결 재수립(TLS) 비용'.
SQL 없이 SELECT 1 만 비교한 7장 (a) 의 HTTP 판본이다.
```

**어디를 보라**: 두 경우 모두 `GET /ping` 을 정확히 200번 했다 — 서버가 한 일은 똑같다. 차이는 오직 **연결을 몇 번 새로 맺었는가**다. `매 요청 새 Client` 는 `with` 블록을 빠져나올 때마다 연결을 닫아, 다음 요청이 **TCP + TLS 핸드셰이크를 200번** 냈다(2995ms). `Client 하나 재사용` 은 첫 요청에서 딱 한 번 연결을 열고 나머지 199번은 그 연결을 **재사용**(135ms). **약 22배**다. 로컬이라 TLS 왕복이 이 정도지, 실제 원격 서버는 네트워크 RTT 까지 붙어 격차가 더 벌어진다. 이게 7장 (a)의 "NullPool 592ms → QueuePool 7.8ms"와 **똑같은 그림**이다.

### (b) keep-alive 를 끄는 두 방법 — 같은 Client 라도 느려진다

파일: [`examples/15_connection_header.py`](examples/15_connection_header.py)

"Client 만 재사용하면 되겠지"가 함정이다. **연결을 실제로 재사용할 수 있어야** 이득이 난다. 같은 Client 를 재사용해도 keep-alive 가 꺼지면 도로 느려진다.

```python
# (1) 기본 재사용 = keep-alive 켜짐 (기준선)
with httpx.Client(verify=cert) as c:
    for _ in range(N): c.get(url)

# (2) 같은 client 지만 매 요청 Connection: close → 서버가 응답 후 연결을 닫음
    c.get(url, headers={"Connection": "close"})

# (3) Limits(max_keepalive_connections=0) → 풀에 유휴 연결을 안 남김
httpx.Client(verify=cert, limits=httpx.Limits(max_keepalive_connections=0))
```

실행:
```bash
uv run --with fastapi --with uvicorn --with httpx --with cryptography python 15_connection_header.py
```

실제 출력:
```
작업: 같은 서버에 GET /ping 150회 순차. 로컬 HTTPS.

             (1) 기본 재사용 (keep-alive) :     81.9 ms
            (2) Connection: close 헤더 :   2208.8 ms
         (3) Limits(max_keepalive=0) :   2199.0 ms

keep-alive 를 끄면 재사용 Client 라도 (2)/(3) 는 (1) 의 약 27~27배로 느려진다.
=> 이득의 열쇠는 'Client 객체'가 아니라 '연결을 실제로 재사용하는가'다.
```

**어디를 보라**: 셋 다 **같은 `httpx.Client` 를 재사용**했는데도, (2)와 (3)은 (1)보다 **27배 느리다**. (2)는 매 요청에 `Connection: close` 를 실어 보내 서버가 응답 직후 연결을 닫게 했다 → 다음 요청은 새 연결(새 TLS). (3)은 httpx 풀에 유휴 keep-alive 연결을 **0개** 남기게 해서, 요청이 끝나면 연결을 폐기 → 매번 새 연결. 즉 keep-alive 는 `Client` 객체를 오래 쥔다고 켜지는 게 아니라 **연결이 살아서 풀에 남아 있어야** 켜진다. (프록시 뒤 keep-alive 불일치도 같은 원리다 — 6절.)

### (c) HTTP/1.1 vs HTTP/2 멀티플렉싱 — 한 연결에 여러 요청

파일: [`examples/15_http2_multiplex.py`](examples/15_http2_multiplex.py)

HTTP/2 는 한 연결 위에서 여러 요청을 **스트림**으로 동시에 실어 나른다. 로컬에서 진짜로 협상시켰다 — 서버는 **hypercorn**(TLS ALPN 으로 HTTP/2 제공, uvicorn 은 h2 미지원), 클라는 `httpx[http2]`. `/slow` 는 0.1초 비동기 대기라 동시성이 드러난다. HTTP/1.1 은 동시 처리를 위해 연결을 여러 개 열어야 하고(각 TLS), HTTP/2 는 **연결 하나**로 끝낸다.

```python
# http2=False → HTTP/1.1, http2=True → HTTP/2 (ALPN 협상)
async with httpx.AsyncClient(http2=http2, verify=cert, limits=limits) as client:
    r0 = await client.get(f"{BASE}/slow")
    version = r0.http_version                       # 'HTTP/1.1' or 'HTTP/2'
    await asyncio.gather(*(client.get(f"{BASE}/slow") for _ in range(50)))
```

실행:
```bash
uv run --with fastapi --with hypercorn --with 'httpx[http2]' --with cryptography python 15_http2_multiplex.py
```

실제 출력:
```
작업: /slow(0.1s 대기) 를 동시에 50개. 로컬 HTTPS(hypercorn).

          HTTP/1.1 (연결 여러 개) :    239.9 ms   협상버전=HTTP/1.1
         HTTP/2 (한 연결 멀티플렉싱) :    148.9 ms   협상버전=HTTP/2

이상적으로 둘 다 ~100ms 에 수렴(모두 동시). 차이는 '연결 수립 개수'에서 온다:
  HTTP/1.1 = 50개 연결(각 TLS 핸드셰이크) / HTTP/2 = 1개 연결에 50개 스트림.
```

**어디를 보라**: `협상버전` 이 실제로 `HTTP/2` 로 찍혔다 — ALPN 으로 진짜 h2 를 맺은 것이다(지어낸 값 아님). 동시 50요청을 HTTP/2 는 **연결 1개**에 50개 스트림으로 처리해 148ms, HTTP/1.1 은 **연결 50개**를 새로 열어(각 TLS 핸드셰이크) 239ms. 이론상 둘 다 `/slow` 대기 0.1초에 수렴해야 하는데, HTTP/1.1 이 더 걸린 건 **50번의 TLS 핸드셰이크 비용**이 얹혔기 때문이다. HTTP/2 의 이득은 "빠른 프로토콜"이라서가 아니라 **연결 수립 횟수를 1로 줄였기 때문**이다(멀티플렉싱). 다만 HTTP/2 가 항상 이기는 건 아니다 — 6절 함정.

### HTTP 버전 비교표

| | HTTP/1.1 | HTTP/2 | HTTP/3 |
|---|---|---|---|
| 전송 계층 | TCP | TCP | **QUIC (UDP 기반)** |
| 연결당 동시 요청 | 1개(순차) | **여러 개(멀티플렉싱)** | 여러 개(멀티플렉싱) |
| 동시 처리 방법 | 연결 여러 개(브라우저 보통 6) | 한 연결에 스트림 다중 | 한 연결에 스트림 다중 |
| Head-of-Line 블로킹 | 있음(요청 순차) | **HTTP 레벨 완화**, 단 TCP 레벨 HOL 잔존 | **TCP HOL 까지 제거**(스트림 독립) |
| 헤더 | 평문, 반복 | HPACK 압축 | QPACK 압축 |
| TLS | 선택(권장) | 사실상 필수(브라우저) | **내장(QUIC=TLS1.3)** |
| keep-alive | 기본 on(`Connection: close`로 off) | 연결 자체가 장수명 | 연결 자체가 장수명, 연결 마이그레이션 |

> **핵심 흐름**: HTTP/1.1 은 "연결을 재사용은 하되(keep-alive) 한 번에 하나씩". HTTP/2 는 "한 연결에 여러 개 동시에(멀티플렉싱)" — 단 밑이 TCP 라 패킷 하나 유실되면 그 연결의 모든 스트림이 함께 멈추는 **TCP HOL** 이 남는다. HTTP/3 은 전송을 **QUIC(UDP 위)** 로 바꿔 스트림을 서로 독립시켜 TCP HOL 까지 없앴다. 셋 다 관통하는 목표는 **"연결을 아껴 쓰기"** 다.

## 5. 우리 코드와 연결

- **매 호출 새 `httpx.Client` = keep-alive 손해**: [`core/speechsuper.py`](../../core/speechsuper.py#L129) 는 발음평가 때 오디오를 받는 `_load_audio` 와 SpeechSuper API 를 치는 `_call_speechsuper` 에서 각각 `with httpx.Client(timeout=_TIMEOUT) as client:` 로 **클라이언트를 매 호출 새로** 만든다([`L231`](../../core/speechsuper.py#L231) 도 동일). 통화후 분석에서 문장이 여러 개면 이 함수가 반복 호출되는데, 그때마다 `api.speechsuper.com` 으로 **TLS 핸드셰이크를 새로** 낸다 — 정확히 (a)의 "재사용 X" 경로다. 발음평가는 통화당 산발적 호출이라 지금 당장 병목은 아니지만, 한 통화에서 문장 N개를 연속 평가한다면 **모듈 레벨에 `httpx.Client` 하나를 만들어 재사용**(또는 `with` 를 루프 바깥으로)하면 (a)만큼의 이득이 난다. "매번 새 클라이언트 만들면 손해"의 실제 사례다.
- **SDK 가 대신 재사용해 주는 경우**: [`core/storage.py`](../../core/storage.py#L41) 는 Supabase `create_client` 결과를 **모듈 전역 `_client` 에 캐시**해 한 번만 만들고([`_get_client`](../../core/storage.py#L28)) Storage 업로드·서명URL·`auth.get_user` 에 재사용한다. [`core/supabase_auth.py`](../../core/supabase_auth.py#L32) 의 토큰 검증도 이 **같은 클라이언트를 재사용**한다(주석: "service_role 클라이언트 재사용"). supabase-py 내부는 httpx 를 쓰므로, 이 싱글턴 덕에 keep-alive 연결이 유지된다 — (a)의 "재사용 O" 경로를 SDK 가 알아서 해 주는 셈. 우리가 직접 `httpx.Client` 를 쓰는 speechsuper 만 재사용 규율에서 벗어나 있다.
- **리버스 프록시 / 로드밸런서 (6·12장과 수미상관)**: Cloud Run 앞단이 사실상 **리버스 프록시 + 로드밸런서**다 — 클라의 TLS 를 종료하고, keep-alive 로 클라와 연결을 유지하며, 뒤의 인스턴스들로 요청을 **분배**하고 오토스케일한다. 즉 **클라↔프록시는 keep-alive 한 연결**, 프록시는 그 위의 요청들을 **인스턴스로 나눈다**(6장 워커, 12장 프로세스 매니저에서 본 "한 대 안에서 나누기"의 바깥 계층). 그래서 우리 인스턴스는 클라이언트와 직접 keep-alive 를 맺기보다 프록시와 맺는다 — 프록시↔인스턴스 구간의 keep-alive/timeout 이 어긋나면 6절의 "프록시 뒤 불일치" 문제가 난다.
- **WebSocket 통화 = 극단적 keep-alive**: [`ws_router.py`](../../domains/learning/realtime/ws_router.py#L35) 의 `/calls/stream` 은 통화 한 건 내내(5분+) **하나의 연결을 계속 열어 둔다**. keep-alive 가 "요청 사이 잠깐 연결 유지"라면, WebSocket 은 한 번 맺은 TCP(+TLS) 연결을 **통화가 끝날 때까지 절대 닫지 않고** 양방향 오디오를 흘린다. 핸드셰이크를 딱 한 번만 내고 그 위에서 수만 개의 오디오 프레임을 주고받으니, keep-alive 의 논리적 극단이다. normalcall 의 2펌프·10분 절대 백스톱(4장·불변식)이 이 "장수명 단일 연결"을 안전하게 유지·종료하는 장치다.

## 6. 흔한 오해 / 함정

- ❌ **"`httpx.Client` 객체만 재사용하면 keep-alive 다."** → (b)처럼 `Connection: close` 나 `max_keepalive_connections=0` 이면 같은 Client 라도 매번 새 연결이다. **연결이 살아서 풀에 남아야** keep-alive다.
- ⚠️ **매 요청 새 소켓 → 포트/TIME_WAIT 고갈**: 연결을 재사용 안 하고 매 요청 열고 닫으면, 닫힌 TCP 연결이 `TIME_WAIT`(보통 수십 초) 로 쌓여 로컬 포트(에페메럴 포트, ~수만 개)가 마른다. 그러면 신규 연결이 실패한다 — 부하가 큰 클라이언트일수록 치명적(9장 부하테스트에서 `httpx.AsyncClient` 를 하나만 재사용한 이유가 이것). keep-alive 는 성능뿐 아니라 **포트 고갈 방지**이기도 하다.
- ⚠️ **keep-alive timeout, 짧아도 길어도 문제**: 서버 keep-alive timeout(uvicorn `--timeout-keep-alive`, 기본 5초)이 **너무 짧으면** 유휴 연결이 금방 닫혀 재핸드셰이크가 잦고, **너무 길면** 안 쓰는 연결이 소켓/메모리를 오래 점유한다. 클라 풀의 만료보다 서버 timeout 이 짧으면, 클라가 "살아 있겠지" 하고 보낸 요청이 이미 닫힌 연결에 떨어져 재시도가 난다.
- ⚠️ **프록시 뒤 keep-alive 불일치**: 클라↔프록시는 keep-alive 인데 프록시↔인스턴스 구간의 timeout 이 더 짧으면, 프록시가 재사용하려던 백엔드 연결이 이미 닫혀 502/504 가 산발한다. Cloud Run·nginx·ALB 처럼 계층이 있으면 **각 구간의 keep-alive timeout 을 아래(백엔드)가 위(프록시)보다 길게** 맞추는 게 정석이다.
- ❌ **"HTTP/2 면 항상 빠르다."** → (c)의 이득은 "연결 수를 1로 줄여서"지 프로토콜이 마법이라서가 아니다. HTTP/2 의 멀티플렉싱은 **HTTP 레벨 HOL 을 완화**할 뿐, 밑의 **TCP 레벨 HOL**(패킷 유실 시 전 스트림 대기)은 남는다 — 그걸 없애려 HTTP/3(QUIC)가 나왔다. 손실 없는 로컬/좋은 네트워크에선 차이가 작을 수도 있다.
- ⚠️ **TLS 재협상 비용**: keep-alive 로 아끼는 가장 큰 비용이 TLS 핸드셰이크다((a) 22배의 대부분). 반대로 연결을 자꾸 새로 열면 이 CPU·왕복 비용이 매번 든다. HTTPS 외부 호출일수록 재사용의 이득이 크다.

## 7. 요약

- HTTP 요청마다 새 TCP + TLS 핸드셰이크를 하면 **연결 수립이 요청 자체보다 비싸다**. **Keep-Alive** = 한 연결을 닫지 않고 여러 요청이 재사용((a): 2995ms → 135ms, 22배). 7장 커넥션 풀의 HTTP 판.
- **이득의 열쇠는 Client 객체가 아니라 '연결의 실제 재사용'**. `Connection: close`·`max_keepalive=0` 이면 같은 Client 도 매번 새 연결((b): 27배 손해).
- **HTTP/1.1**: 연결당 순차(동시성 위해 연결 여럿). **HTTP/2**: 한 연결 멀티플렉싱((c): 연결 1개로 동시 50요청, h2 실측). **HTTP/3**: QUIC(UDP)로 TCP HOL 까지 제거.
- **우리 코드**: `speechsuper.py` 는 매 호출 새 `httpx.Client`(재사용하면 이득), `storage.py`/`supabase_auth.py` 는 SDK 클라이언트를 **전역 재사용**. Cloud Run 앞단이 **리버스 프록시/LB**(클라와 keep-alive, 인스턴스로 분배). **WS 통화**는 5분+ 단일 연결 = 극단적 keep-alive.
- **함정**: 재사용 안 하면 포트/TIME_WAIT 고갈, keep-alive timeout 은 짧아도 길어도 손해, 프록시 뒤 timeout 불일치는 502, HTTP/2 가 만능은 아니다(TCP HOL 잔존).

## 8. 연습문제

1. `15_keepalive_reuse.py` 에서 서버를 HTTPS 가 아니라 **평문 HTTP**(uvicorn 의 `--ssl-*` 제거)로 띄우면 "재사용 X" vs "재사용 O" 시간차는 어떻게 될까? 왜 HTTPS 일 때 차이가 훨씬 클까?
2. `15_connection_header.py` 의 (2)/(3)이 (1)보다 27배 느린 이유를, (a)의 "TLS 핸드셰이크 200회 vs 1회"와 연결해 한 문장으로 설명하라.
3. 우리 `speechsuper.py` 가 한 통화에서 문장 20개를 연속 평가한다고 하자. 지금 코드(`with httpx.Client()` 를 함수마다)와, 모듈 전역에 `httpx.Client` 하나를 두고 재사용하는 코드 중 어느 쪽이 얼마나 유리한가? (a)의 결과로 근사해 보라.

<details>
<summary>답</summary>

1. 평문 HTTP 로 바꾸면 두 경우의 차이가 **크게 줄어든다**(로컬 TCP 핸드셰이크는 수 µs~수십 µs 로 매우 싸다). HTTPS 일 때 차이가 큰 건 새 연결마다 **TLS 핸드셰이크**(인증서 교환·키 합의로 왕복 1~2회 + 비대칭 암호 CPU 연산)가 붙기 때문 — keep-alive 가 아끼는 비용의 대부분이 이 TLS 부분이다. 그래서 원격 https 외부 호출일수록 재사용 이득이 크다.
2. (2)/(3)은 keep-alive 를 꺼 매 요청 새 연결 = **150번의 TLS 핸드셰이크**를 냈고, (1)은 첫 요청 **1번**만 냈다 — (a)에서 본 "200회 vs 1회"와 정확히 같은 구조라, 핸드셰이크 횟수 비율만큼 느려진 것이다.
3. 지금 코드는 문장 20개면 SpeechSuper 로 **TLS 핸드셰이크를 최소 20번**(+오디오 로드까지 http면 더) 낸다. 전역 재사용이면 **1번**이면 끝. (a)의 비율(재사용이 ~22배)을 대입하면, 연결 수립이 지배적인 구간에서 재사용 쪽이 크게 유리하다 — 다만 SpeechSuper 응답 자체(발음 채점)가 오래 걸리면 총시간에서 핸드셰이크 비중은 줄어드니, 실제 이득은 "핸드셰이크 시간 / 총 요청 시간" 비율만큼이다. 그래도 포트/TIME_WAIT 관점에서도 재사용이 옳다.

</details>

---

## 교과서를 마치며 — 15장의 여정

우리는 **밑바닥부터 꼭대기까지 한 줄로** 올라왔다. **파이썬 런타임**(01 GIL → 02 이벤트 루프 → 03 코루틴/asyncio)에서 "왜 async 인가"를 배우고, **웹서버가 요청을 받는 법**(04 ASGI → 05 Uvicorn → 06 워커)으로 FastAPI 가 선 땅을 봤다. **자원 재사용과 측정**(07 커넥션 풀 → 08 프로파일링)에서 "연결은 아끼고 성능은 숫자로", **부하와 한계**(09 부하테스트)로 터지기 전에 터뜨려 보고, **OS/CPU 밑바닥**(10 컨텍스트 스위칭·캐시 → 11 epoll)에서 이벤트 루프의 진짜 엔진을 확인했다. **프로덕션 스케일**(12 Gunicorn → 13 멀티프로세싱 → 14 Redis 캐시 → 15 HTTP Keep-Alive)에서 코어를 다 쓰고, 계산을 병렬화하고, 안 하는 계산이 제일 빠르며, **연결을 아껴 쓰는 법**으로 교과서를 닫는다. 7장에서 "연결은 비싸니 풀로 재사용하라"고 배운 것을, 15장에서 네트워크 연결로 **수미상관**하며 마무리했다.

**다음 스텝 — 부록(심화)**: 여기까지가 "한 서비스를 빠르고 안정적으로 돌리는" 토대다. 그 위에는 **직렬화**(orjson/Protobuf — 응답 인코딩 비용), **메시지 큐**(Kafka/RabbitMQ/Redis Streams — 통화후 분석을 비동기 파이프라인으로), **컨테이너/K8s**(Docker/Pod/HPA — Cloud Run 너머의 오케스트레이션), **모니터링**(Prometheus/Grafana/OpenTelemetry/Jaeger — 8장 측정의 상시화), **분산 시스템**(Sharding/Replication/Consistent Hashing — 여러 대로 나누기)이 있다. 필요할 때 부록으로 이어 가자.

축하한다 — 15장을 모두 마쳤다. 🎉

---

이전 챕터 ← [14. Redis Cache — 안 하는 게 제일 빠른 계산](14-redis-cache.md)

다음 챕터 → **없음 (완결).** 위 "다음 스텝"의 부록으로 심화를 이어 가세요.

돌아가기 → [교과서 목차(README)](README.md)
