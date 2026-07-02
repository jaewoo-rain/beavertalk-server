# 8장. 실시간 한국어 회화 통화 (플래그십)

> 📘 **이 장을 읽고 나면**
> - 학습 도메인의 데이터 모델(Call · CallRawData · Sentence · Evaluation · Review · Level)이 서로 어떻게 연결되어 통화 한 건을 표현하는지 그림으로 떠올릴 수 있어요.
> - 클라이언트가 WebSocket 을 열면 **4개의 async 펌프**가 동시에 돌면서 오디오·전사·시계·저장을 병렬로 처리하는 실시간 통화의 심장부를 이해할 수 있어요.
> - 시스템 프롬프트 조립(persona_prompt) → Gemini Live 세션(gemini_live) → 통화후 분석(gemini_analysis) 로 이어지는 AI 파이프라인 전체 흐름을 설명할 수 있어요.
> - 통화가 끝난 뒤 백그라운드에서 표현을 뽑고, TTS 를 합성하고, 발음을 채점하는 과정을 코드로 따라갈 수 있어요.
> - "async 컨텍스트에서는 ORM 객체를 들고 다니지 않는다"는 이 프로젝트의 중요한 규율이 왜 있는지 알 수 있어요.

---

## 들어가기 전에 — 이 장이 왜 제일 어려운가

지금까지(1~3장)는 "요청 하나 들어오면 → DB 조회 → 응답 하나 나간다" 는 **평범한 REST** 였어요. Spring 으로 치면 `@RestController` 하나에 `@GetMapping` 하나, 딱 그 느낌이죠.

그런데 이 장의 주인공인 **실시간 통화**는 완전히 다른 세계입니다. 비유하자면 이렇습니다.

- **REST** = 편지 주고받기. 한 통 보내면 한 통 온다. 끝나면 봉투를 닫는다.
- **실시간 통화** = 진짜 전화 통화. 내가 말하는 동안 상대도 듣고 있고, 나는 상대 목소리를 실시간으로 듣고, 통화 시간을 재는 시계도 돌고 있고, 그 와중에 통화 내용을 몰래 녹음까지 하고 있다. **이 모든 게 "동시에" 일어난다.**

Java 에서 이런 걸 하려면 스레드 여러 개를 띄우고 `synchronized` 로 씨름해야 했을 거예요. Python 은 이걸 **asyncio** 라는 도구로, 스레드 대신 "협조적으로 번갈아 실행되는 코루틴" 으로 풉니다. 겁먹지 마세요. 하나씩 그림으로 풀어갈게요.

---

## 8.1 먼저 지도 — 통화 한 건이 남기는 데이터

### 왜 필요한가

통화가 끝나면 사용자에게 보여줄 게 많습니다. "오늘 무슨 얘기 했지?"(요약), "내가 배운 표현이 뭐였지?"(문장들), "내 발음 점수는?"(평가), "다시 녹음해서 연습해볼까?"(복습). 이 각각을 담을 테이블이 필요해요.

### 비유

통화 한 건을 **학교 수업 한 시간**이라고 생각해 보세요.

- **Call** = 수업 그 자체(언제, 누구랑, 몇 분, 상태).
- **CallRawData** = 수업 녹취록(누가 몇 번째로 무슨 말을 했나 — 턴별 원본).
- **Sentence** = 그 수업에서 "배운 표현" 만 골라낸 단어장.
- **Evaluation** = 단어장의 각 표현에 매긴 점수표(1:1).
- **Review** = 학생이 그 표현을 집에서 다시 소리내 읽어본 녹음 시도들(1:N).
- **Level** = 학생의 반 편성(1~12레벨) — 수업 난이도를 정하는 기준.

### 관계도

```
Member(회원)
  └─< Call (통화 루트, status: ongoing→analyzing→done/failed, mode)
        ├─< CallRawData (턴별 전사+오디오 key)   ← 통화중 계속 쌓임
        └─< Sentence (배운 표현, source_type, 소프트삭제)  ← 통화후 분석이 생성
              ├─ Evaluation (1:1, 발음/유창성/리듬 점수)
              └─< Review (복습 녹음 시도, 채점 JSON)
Level (1~12 마스터 데이터)  ← member.korean_level 이 가리킴
```

### 각 모델의 핵심만

**Call** — 통화의 루트. `status` 컬럼이 통화의 생애를 표현하는 상태머신입니다.

```
ongoing   (통화 진행 중)
   ↓ 통화 끝, 세그먼트 저장
analyzing (백그라운드 분석 중)
   ↓ 분석 성공          ↓ 분석 실패/예외
 done                 failed
```

- `mode`: 분석이 판정한 통화 성격. **주의** — 모델 컬럼 주석에는 `conversation/study/unknown` 이라 적혀 있지만, 실제로 저장되는 값은 분석 스키마가 뱉는 `study/chat/mixed` 입니다(아래 8.5 참고). 주석이 살짝 오래됐어요.
- 실제 코드: [domains/learning/models/call.py:37](../../domains/learning/models/call.py#L37) (status), [domains/learning/models/call.py:41](../../domains/learning/models/call.py#L41) (mode)

**CallRawData** — 턴 하나의 원본. `role`(user/beaver), `turn_index`(0부터), `content`(전사 텍스트), `voice_url`(오디오 object key). 실제 코드: [domains/learning/models/call_raw_data.py:23](../../domains/learning/models/call_raw_data.py#L23)

**Sentence** — 배운 표현 한 건. `source_type` 이 표현의 출처를 나눕니다.
- `asked`: 학습자가 "○○ 한국어로 어떻게 말해요?" 물어서 배운 것
- `corrected`: 학습자의 어색한 발화를 비버가 고쳐준 것
- `drilled`: 공부 모드에서 따라 말한 것

그리고 **소프트 삭제** — 사용자가 표현을 지워도 행을 물리적으로 지우지 않고 `deleted_at` 시각만 찍습니다(읽기에서 제외). 실제 코드: [domains/learning/models/sentence.py:33](../../domains/learning/models/sentence.py#L33) (source_type), [domains/learning/models/sentence.py:38](../../domains/learning/models/sentence.py#L38) (deleted_at)

**Evaluation** — Sentence 와 **1:1**. `sentence_id` 에 `unique=True` FK 를 걸어 "발화당 평가 1건" 을 DB 레벨에서 보장합니다. 실제 코드: [domains/learning/models/evaluation.py:21](../../domains/learning/models/evaluation.py#L21)

**Review** — Sentence 와 **1:N**. 복습 녹음 시도마다 한 행. 채점 결과는 `feedback` JSON 컬럼에 통째로. 실제 코드: [domains/learning/models/review.py:24](../../domains/learning/models/review.py#L24)

**Level** — 12단계 마스터 데이터. 통화 프롬프트의 `[학습자 수준]` 슬롯에 넣을 `profile` 문자열을 갖고 있어요. 실제 코드: [domains/learning/models/level.py:35](../../domains/learning/models/level.py#L35)

### 흔한 함정

Spring/JPA 습관으로 "연관을 lazy 로 걸어두고 필요할 때 `call.getSentences()` 하면 되겠지" 라고 생각하기 쉬운데, **실시간 통화 코드(async)에서는 이게 지뢰**입니다. 8.6 에서 자세히 다룹니다.

> 한 줄 요약: 통화 한 건은 Call(루트) → CallRawData(녹취) → Sentence(단어장) → Evaluation(점수)/Review(복습) 로 뻗어나가고, Level 은 난이도 기준으로 옆에서 참조됩니다.

---

## 8.2 통화가 시작되는 순간 — WebSocket 진입과 인증

### 왜 필요한가

REST 는 매 요청마다 헤더에 토큰을 실어 보냅니다. 그런데 WebSocket 은 "한 번 연결하면 계속 열려 있는 파이프" 예요. FastAPI 의 `HTTPBearer` 의존성(Depends)은 WebSocket 핸드셰이크에는 그대로 못 붙습니다. 그래서 토큰을 **쿼리스트링**으로 받아 직접 검증합니다.

### 흐름

```
클라이언트
  │  ws://.../api/v1/calls/stream?token=<Supabase access token>
  ▼
ws_router.ws_call_stream()
  │  ① token 을 쿼리에서 꺼낸다
  │  ② verify_token(token) → auth_user (실패면 close 1008, accept 안 함)
  │  ③ accept()  ← 여기서 비로소 연결 수락
  │  ④ genai_client / settings 없으면 ServerError 보내고 close
  │  ⑤ auth uuid → member find-or-create → member_id
  ▼
run_call(websocket, settings, client, session_factory, member_id=...)  ← 통화 본체로 위임
```

포인트 두 가지:
- `verify_token` 은 네트워크 호출(Supabase 에 물어봄)이라 **이벤트 루프를 막지 않도록** `run_in_threadpool` 로 감쌉니다. 실제 코드: [domains/learning/realtime/ws_router.py:44](../../domains/learning/realtime/ws_router.py#L44)
- 인증 실패 시 close 코드 **1008**(정책 위반). 실제 코드: [domains/learning/realtime/ws_router.py:45](../../domains/learning/realtime/ws_router.py#L45)

인증에 성공한 뒤에야 `run_call` 로 넘깁니다. 실제 코드: [domains/learning/realtime/ws_router.py:70](../../domains/learning/realtime/ws_router.py#L70)

### 흔한 함정

`accept()` 를 인증보다 **먼저** 부르면, 아직 자격 없는 상대와 연결이 성립된 뒤에 끊는 꼴이 됩니다. 이 코드는 일부러 인증 → (실패면 accept 없이 close) → 성공해야 accept 순서를 지킵니다.

> 한 줄 요약: WS 는 토큰을 쿼리로 받아 `verify_token` 으로 검증하고, 통과해야 `accept()` 후 `run_call` 로 통화 본체에 넘깁니다.

---

## 8.3 심장부 — 4개 펌프가 동시에 도는 실시간 통화

이 절이 이 프로젝트에서 **가장 중요하고 가장 어려운** 부분입니다. 천천히 그림부터 봅시다.

### 큰 그림

`run_call` 은 준비 작업(프롬프트 조립, 통화 행 생성)을 마친 뒤, `_run_session` 안에서 **`asyncio.TaskGroup`** 으로 4개의 작업을 **동시에** 띄웁니다.

```
                    ┌─────────────────────────────────────────┐
                    │        asyncio.TaskGroup (4개 동시)        │
   클라 마이크 ─────▶│ ① _pump_client_to_gemini                 │───▶ Gemini Live
   (PCM 16k)        │    (내 목소리를 AI 로, barge-in off)       │
                    │                                           │
   클라 스피커 ◀────│ ② _pump_gemini_to_client                 │◀─── Gemini Live
   (PCM 24k+전사)   │    (AI 목소리+전사를 클라로 relay)         │     (오디오+전사)
                    │                                           │
                    │ ③ _watch_call_clock                       │
                    │    (5분→종료시드 / 10분→강제종료 시계)      │
                    │                                           │
                    │ ④ _periodic_flush                         │───▶ DB (60초마다)
                    │    (누적 세그먼트를 주기적으로 저장)         │
                    └─────────────────────────────────────────┘
                         공유 상태 _CallState (segments 등)
```

실제 코드: TaskGroup 정의 [domains/learning/realtime/call_session.py:276](../../domains/learning/realtime/call_session.py#L276)

> 📌 **참고(문서 드리프트):** 파일 맨 위 docstring 에는 "2펌프" 라고 적혀 있어요([call_session.py:9](../../domains/learning/realtime/call_session.py#L9)). 이건 원본(beavertalk bridge.py)에서 포팅할 때의 옛 설명이고, 지금 코드는 위 그림처럼 **4개** 를 띄웁니다. 코드가 진실입니다.

### asyncio.TaskGroup 이 뭔가요? (Java 개발자용)

Java 의 `ExecutorService` + `invokeAll` 과 비슷하지만 더 안전합니다.

- `async with asyncio.TaskGroup() as tg:` 블록에 들어가면, `tg.create_task(...)` 로 만든 작업들이 동시에 돕니다.
- 블록을 **빠져나갈 때** 모든 작업이 끝날 때까지 기다립니다(자동 join).
- 그중 하나라도 예외를 던지면 **나머지를 전부 취소** 하고 예외를 묶어서(`ExceptionGroup`) 올립니다. 그래서 `except* _CallFinished:` 처럼 별표(`*`) 붙은 except 로 받습니다.

즉 "한 통화의 네 일꾼은 함께 살고 함께 죽는다" 는 구조예요. 실제 코드: [domains/learning/realtime/call_session.py:284](../../domains/learning/realtime/call_session.py#L284)

### 펌프 ① 클라 → Gemini

내 마이크 오디오(PCM 16bit/16kHz/mono)를 그대로 Gemini 로 흘려보냅니다. 핵심은 **barge-in off**:

- 비버(AI)가 말하는 중(`state.turn_id` 가 있음)에는 내 마이크를 **안 보냅니다.** 서로 말이 겹치는 걸 막는 거예요. 통화라기보단 "한 명씩 말하는 워키토키" 에 가깝습니다.

실제 코드: [domains/learning/realtime/call_session.py:334](../../domains/learning/realtime/call_session.py#L334) (`data and state.turn_id is None` 조건이 barge-in off 의 정체)

### 펌프 ② Gemini → 클라

AI 응답 스트림을 받아 **즉시 클라로 forward** 하면서(반응성 우선), 동시에 상태머신을 돌립니다. Gemini 가 보내는 이벤트는 4종류입니다.

| 이벤트 kind | 의미 | 서버가 클라에 보내는 프레임 |
|---|---|---|
| `audio` | AI 음성 청크(24k) | 바이너리(오디오) + 새 턴이면 `turn_start` |
| `in_tr` | 내 말의 전사 | `input_transcript` |
| `out_tr` | AI 말의 전사 | `output_transcript` |
| `turn_end` | AI 한 턴 종료 | `turn_end` |

각 이벤트를 forward 하는 곳: [domains/learning/realtime/call_session.py:399](../../domains/learning/realtime/call_session.py#L399) (`_forward_event`)

그리고 이 펌프가 **5분 종료 로직** 도 담당합니다(아래 ③ 시계와 협업).

### 펌프 ③ 통화 시계

시간 상수부터 봅시다. 실제 코드: [domains/learning/realtime/call_session.py:43](../../domains/learning/realtime/call_session.py#L43)

| 상수 | 값 | 의미 |
|---|---|---|
| `CALL_DURATION_S` | 300초(5분) | 첫 발화부터 5분 → **종료 시드** 주입(자연스러운 작별 유도) |
| `ABSOLUTE_CALL_TIMEOUT_S` | 600초(10분) | 절대 상한. 넘으면 무조건 강제 종료(백스톱) |
| `SEED_TO_HANGUP_S` | 12초 | 종료 시드 후 안 끝나면 강제 종료까지 여유 |
| `FLUSH_INTERVAL_S` | 60초 | 세그먼트 중간 저장 주기 |

시계의 역할은 단순합니다: 5분이 지나면 `should_close = True` 플래그만 세웁니다. **직접 종료시키지 않아요.** 실제 코드: [domains/learning/realtime/call_session.py:441](../../domains/learning/realtime/call_session.py#L441)

그 플래그를 보고 실제로 종료 시드(`_CLOSE_SEED`, "통화 시간이 다 됐다. 자연스럽게 작별 인사...")를 주입하는 건 펌프 ②입니다. **왜 이렇게 나눴을까요?** 시드는 반드시 "AI 가 한 턴을 끝낸 경계(turn_end)" 에 넣어야 말이 안 끊기기 때문이에요. 시계는 시간만 재고, 실제 주입은 대화 흐름을 아는 펌프 ②가 담당 — **역할 분리**입니다. 실제 코드: [domains/learning/realtime/call_session.py:389](../../domains/learning/realtime/call_session.py#L389)

이중 안전망:
- 부드러운 종료(5분 시드) → AI 가 스스로 작별하고 turn_end → `_CallFinished`
- 그래도 안 끝나면(시드 후 12초) 시계가 강제 `_CallFinished`
- 그마저도 뚫리면 `run_call` 을 감싼 `asyncio.timeout(600초)` 이 최후의 백스톱. 실제 코드: [domains/learning/realtime/call_session.py:187](../../domains/learning/realtime/call_session.py#L187)

### 펌프 ④ 60초 주기 저장

통화가 5~10분이라 길고, 중간에 서버가 죽을 수도 있습니다. 그래서 **메모리에만 쌓지 않고** 60초마다 지금까지의 세그먼트를 DB 에 흘려보냅니다(점진 flush). `persisted_count` 라는 커서로 "어디까지 저장했나" 를 추적해 중복 저장을 피해요. 실제 코드: [domains/learning/realtime/call_session.py:290](../../domains/learning/realtime/call_session.py#L290)

### 세그먼트는 어떻게 쌓이나

두 펌프가 `_CallState` 라는 공유 객체에 조각을 모읍니다([call_session.py:68](../../domains/learning/realtime/call_session.py#L68)). 한 턴이 끝나면 `_flush_user_segment` / `_flush_beaver_segment` 가 "현재 누적 중이던 PCM+텍스트" 를 `segments` 리스트의 한 항목으로 확정합니다.

```
말하는 중 ─ cur_user_pcm/text 에 계속 append
turn 경계 ─ _flush_*_segment() 로 확정 → segments 에 push, 버퍼 비움
60초마다 ─ segments[persisted_count:] 를 DB 로 flush
```

실제 코드: [domains/learning/realtime/call_session.py:101](../../domains/learning/realtime/call_session.py#L101)

### 통화 종료 시퀀스

`run_call` 의 `finally` 블록이 정리를 책임집니다. 실제 코드: [domains/learning/realtime/call_session.py:208](../../domains/learning/realtime/call_session.py#L208)

```
finally:
  1. 남은 user/beaver 세그먼트 확정
  2. _persist_remaining      → 아직 저장 안 한 세그먼트 저장 + status="analyzing"
  3. _trigger_analysis       → 백그라운드 분석 task 시작 (non-blocking!)
  4. _finish_call            → call_ended 프레임 전송 → playback_done ack 대기 → WS close
```

`_finish_call` 은 클라에 `call_ended` 를 보내고, 클라가 "마지막 오디오 다 재생했어요(`playback_done`)" 라고 알려줄 때까지 최대 2초 기다린 뒤 곱게 끊습니다. 실제 코드: [domains/learning/realtime/call_session.py:461](../../domains/learning/realtime/call_session.py#L461)

### 흔한 함정

`_trigger_analysis` 가 만든 task 는 반드시 **강한 참조** 로 붙들어야 합니다(`_analysis_tasks` set). 안 그러면 파이썬 GC 가 "아무도 안 붙잡네?" 하고 실행 도중 task 를 수거해버릴 수 있어요. 실제 코드: [domains/learning/realtime/call_session.py:57](../../domains/learning/realtime/call_session.py#L57), [call_session.py:222](../../domains/learning/realtime/call_session.py#L222)

> 한 줄 요약: 한 통화는 TaskGroup 안에서 마이크·스피커·시계·저장 4개 펌프가 공유 `_CallState` 위에서 동시에 돌고, 5분/10분 이중 시계와 60초 주기 저장으로 안전하게 시작·유지·종료됩니다.

---

## 8.4 AI 파이프라인 — 프롬프트 조립 → Live 세션 → 선톡

### 왜 필요한가

Gemini 에게 "그냥 한국어 선생님 해줘" 라고만 하면 매번 성격이 달라지고, 학습자 레벨도 무시합니다. 그래서 **통화를 열기 전에** 캐릭터 성격 + 학습자 레벨 + 관심사 + 지난 이력을 한 덩어리 시스템 지시문으로 조립해 넣습니다.

### 비유

배우에게 대본을 주는 것과 같아요. persona_prompt 는 **대본 작가**입니다. 다만 이 작가는 창작을 하지 않아요 — 정해진 템플릿 슬롯에 조각들을 **끼워 맞추기만** 합니다(LLM 생성 0).

### 조립 흐름

```
load_call_setup(db, member_id, character_id)   ← 통화 시작 전 DB 1회 조회
  → { role, personality, rules,      (캐릭터 = Character 테이블)
      level_profile,                 (레벨 = Level.profile)
      locale, interests, name,       (회원 = Member + member_reason)
      history, voice }               (지난 통화 요약 + 배운 표현)
        │
        ▼
build_system_instruction(...)
  = 불변 규칙 템플릿
  + 페르소나(role/personality/rules)
  + [학습자 수준] level_profile
  + [학습자 흥미] interests
  + (있으면) [최근 학습 이력]
        │
        ▼
open_session(client, settings, system_instruction=..., voice=...)
        │
        ▼
session.send_text_turn(SEED_OPENING)   ← "선톡": AI 가 먼저 전화 걸어 말 시작
```

실제 코드:
- 조립 함수: [core/persona_prompt.py:78](../../core/persona_prompt.py#L78)
- 불변 규칙 템플릿(모드 분기·종료 규약·코드스위칭 등이 여기 다 있음): [core/persona_prompt.py:32](../../core/persona_prompt.py#L32)
- 셋업 로딩(DB → 평범한 dict): [domains/learning/service/normalcall_service.py:69](../../domains/learning/service/normalcall_service.py#L69)
- 선톡 시드 상수: [core/persona_prompt.py:25](../../core/persona_prompt.py#L25)
- 선톡 주입 위치: [domains/learning/realtime/call_session.py:283](../../domains/learning/realtime/call_session.py#L283)

### Gemini Live 세션 설정 — 5분 통화의 비밀

`build_live_config` 가 만드는 `LiveConnectConfig` 에 이 통화를 통화답게 만드는 설정이 다 들어 있습니다. 실제 코드: [core/gemini_live.py:53](../../core/gemini_live.py#L53)

- `response_modalities=["AUDIO"]` — 텍스트가 아니라 **음성** 으로 응답.
- `input_audio_transcription` / `output_audio_transcription` — 내 말과 AI 말을 **전사** 해서 함께 받음(통화후 분석 재료).
- `speech_config` 의 prebuilt voice — 캐릭터별 목소리(기본 `"Fenrir"`).
- **`context_window_compression` (슬라이딩 윈도우)** — 이게 핵심입니다. 오디오는 토큰을 엄청 잡아먹어서, 압축 없이는 2분쯤 지나면 컨텍스트 한계로 서버가 세션을 닫아버려요. 슬라이딩 윈도우로 오래된 맥락을 압축해 **5분+ 통화** 를 가능하게 합니다. 실제 코드: [core/gemini_live.py:75](../../core/gemini_live.py#L75)

세션을 여는 async 컨텍스트 매니저: [core/gemini_live.py:162](../../core/gemini_live.py#L162)

### SEED_OPENING vs _CLOSE_SEED

두 개의 "시드(seed)" 가 통화의 시작과 끝을 만듭니다.

- **SEED_OPENING**(선톡): 통화가 열리자마자 AI 에게 "네가 먼저 전화 건 상황이야, 인사하고 공부할지 대화할지 물어봐" 라고 넣는 첫 user 턴. persona_prompt 가 소유. [core/persona_prompt.py:25](../../core/persona_prompt.py#L25)
- **_CLOSE_SEED**(종료 시드): 5분 지나면 "시간 다 됐으니 자연스럽게 작별해" 라고 넣는 마지막 트리거. call_session 이 소유. [core/persona_prompt.py:9](../../core/persona_prompt.py#L9) 의 주석대로 "종료 시드는 호출부가 소유" 하며 실제 값은 [call_session.py:50](../../domains/learning/realtime/call_session.py#L50).

### 흔한 함정

프롬프트에서 "통화 종료 시점은 서버가 정한다, AI 는 시간을 언급하지 마라" 를 강하게 못박아 두었습니다([persona_prompt.py:45](../../core/persona_prompt.py#L45)). 이게 없으면 AI 가 멋대로 "이제 슬슬 끊을까요?" 하며 5분 규칙을 깨버려요.

> 한 줄 요약: 통화 전 DB 조각들을 persona_prompt 가 대본으로 조립하고, gemini_live 가 오디오+전사+컨텍스트압축 세션을 열며, SEED_OPENING 으로 AI 가 먼저 말을 겁니다.

---

## 8.5 통화후 분석 — 백그라운드에서 벌어지는 일

### 왜 필요한가

통화 도중에 "방금 배운 표현 정리해줘" 를 하면 통화가 버벅입니다. 그래서 통화가 끝난 **직후 백그라운드 task** 로 미뤄서, 전사를 다시 읽고 표현을 뽑고 점수 틀을 만들고 음성을 합성합니다.

### 흐름

`analyze_call` 이 전 과정을 오케스트레이션합니다. 실제 코드: [domains/learning/service/normalcall_service.py:319](../../domains/learning/service/normalcall_service.py#L319)

```
analyze_call(call_id, client, settings, session_factory, locale)
  1. _build_dialog       CallRawData 들을 turn 순서로 [USER]/[BEAVER] 대화문 재구성
  2. (대화 비었으면 → status=done, 조기 종료)
  3. gemini_analysis.generate_structured(schema=CallAnalysis)
        → summary + detected_mode + expressions[] 를 JSON 으로 구조화 출력
  4. _save_analysis      Call.summary/mode 저장 + 표현마다 Sentence 생성
                         (각 Sentence 에 placeholder Evaluation() 붙임 — 점수는 None)
  5. 표현마다:
        tts.synthesize_korean(korean) → 오디오 bytes
        storage.upload(SAMPLES 버킷)  → object key
        storage.public_url(...)       → 재생 URL
        _set_sentence_tts             → Sentence.voice_url 갱신
  6. status=done
```

### 구조화 출력이란

Gemini 에게 그냥 물으면 자유 텍스트가 옵니다. 대신 **Pydantic 스키마를 response_schema 로 넘기면** 정해진 JSON 형태로 강제할 수 있어요. 여기서 쓰는 스키마:

- `CallAnalysis`: `summary`(요약) + `detected_mode`(study/chat/mixed) + `expressions[]`. 실제 코드: [domains/learning/service/normalcall_service.py:230](../../domains/learning/service/normalcall_service.py#L230)
- `LearnedExpression`: `korean` + `translation` + `source_type`(asked/corrected/drilled) + `learner_attempt`. 실제 코드: [domains/learning/service/normalcall_service.py:221](../../domains/learning/service/normalcall_service.py#L221)

호출 메커니즘 자체(generateContent + response_schema)는 core 어댑터에 있습니다: [core/gemini_analysis.py:25](../../core/gemini_analysis.py#L25)

여기서 **역할 분리** 를 다시 봅시다: "무엇을 분석하나(프롬프트·스키마)" 는 도메인 지식이라 서비스가 소유하고, "어떻게 LLM 을 부르나" 는 core 가 담당합니다.

### placeholder Evaluation 이 왜 있나

분석 단계에서 표현을 뽑을 때는 아직 **발음 점수가 없습니다**(사용자가 아직 연습 안 함). 그래서 빈 `Evaluation()` 을 미리 붙여두고, 나중에 복습(8.7)에서 점수를 채웁니다. 실제 코드: [domains/learning/service/normalcall_service.py:301](../../domains/learning/service/normalcall_service.py#L301)

### 전 과정이 graceful

이 백그라운드 작업은 **어떤 단계가 실패해도 통화 자체엔 영향이 없습니다.** 전사가 비면 done(빈 결과), 분석 호출이 실패하면 failed, TTS 가 실패하면 표현은 저장되되 음성만 없음. 가장 바깥 `try/except` 가 모든 예외를 흡수합니다. 실제 코드: [domains/learning/service/normalcall_service.py:379](../../domains/learning/service/normalcall_service.py#L379) (자세한 철학은 9장에서)

### 흔한 함정

TTS 합성 결과를 저장하는 `run_db` 람다에서 루프 변수를 그대로 캡처하면 마지막 값만 남는 파이썬 클로저 함정이 있어요. 코드는 `lambda db, sid=sentence_id, u=url:` 처럼 **기본 인자로 값을 고정** 해 이걸 피합니다. 실제 코드: [domains/learning/service/normalcall_service.py:374](../../domains/learning/service/normalcall_service.py#L374)

> 한 줄 요약: 통화가 끝나면 백그라운드 task 가 전사를 재구성해 구조화 분석(CallAnalysis)으로 표현을 뽑고, Sentence(+빈 Evaluation)를 만들고, 표현마다 TTS 를 합성해 저장한 뒤 status=done 을 찍습니다.

---

## 8.6 중요한 규율 — async 컨텍스트에서 ORM 을 들고 다니지 마라

### 왜 필요한가

이 프로젝트의 DB 는 **동기(sync) SQLAlchemy** 입니다. 그런데 통화 코드는 **비동기(async)** 예요. 둘을 섞는 순간 위험이 생깁니다.

SQLAlchemy 의 ORM 객체는 lazy loading 이라, `call.sentences` 같은 속성에 접근하는 그 순간 DB 쿼리가 튀어나갑니다. 이게 async 이벤트 루프 위에서 벌어지면 이벤트 루프를 막거나, 이미 닫힌 세션을 건드려 터집니다.

### 비유

ORM 객체는 "콘센트에 꽂혀 있어야만 작동하는 가전제품" 이에요(세션에 붙어 있어야 lazy load 가 됨). 그걸 콘센트(세션)에서 뽑아 다른 방(async 루프)으로 들고 가면, 버튼을 누르는 순간 안 켜지거나 스파크가 튑니다.

### 이 프로젝트의 해법

**세션 안에서 필요한 값을 전부 평범한 dict/str 로 꺼낸 다음, 그것만 async 세계로 넘긴다.**

- `load_call_setup` 은 Member/Level/Character 를 조회하지만, 반환은 **ORM 이 아니라 평범한 값만 담은 dict** 입니다. 주석에도 "ORM 객체가 아니라 평범한 값만 담아 async 컨텍스트로 안전히 넘긴다" 라고 못박아 뒀어요. 실제 코드: [domains/learning/service/normalcall_service.py:73](../../domains/learning/service/normalcall_service.py#L73)
- 모든 동기 DB 접근은 `run_db` 로 감쌉니다 — 별도 스레드에서 **짧게 세션을 열고 닫는** 헬퍼. 장수명 WS 가 세션을 오래 붙들지 않게 하는 게 목적입니다. 실제 코드: [domains/learning/service/normalcall_service.py:50](../../domains/learning/service/normalcall_service.py#L50)

```
async 통화 코드
   │  await run_db(factory, lambda db: svc.load_call_setup(db, ...))
   ▼
run_in_threadpool 안:
   db = factory()          ← 새 세션 열기
   result = fn(db)         ← 이 안에서만 ORM 사용, 평범한 값으로 변환
   db.close()              ← 즉시 닫기
   ▼
async 세계로는 "평범한 값" 만 돌아온다  ← 안전
```

### 흔한 함정

"`load_call_setup` 이 `Member` 객체를 그냥 return 하면 편하지 않나?" — 안 됩니다. 그 순간 콘센트 뽑힌 가전제품을 넘기는 거예요. 반드시 세션 안에서 `member.name`, `member.language` 같은 스칼라로 뽑아 넘기세요.

> 한 줄 요약: sync DB + async 통화의 조합에서는 ORM 객체를 async 세계로 넘기지 말고, `run_db`(짧은 세션 스레드) 안에서 평범한 값으로 변환해 넘깁니다.

---

## 8.7 복습과 발음 채점

### 왜 필요한가

배운 표현을 눈으로 보는 것만으론 부족하죠. 사용자가 직접 소리내 읽고, "내 발음이 몇 점인지" 글자 단위로 받아봐야 학습이 됩니다.

### 흐름

```
① 북마크          Sentence.is_bookmarked 토글
                 SentenceService.set_bookmark → [sentence_service.py:27]

② 복습 녹음 제출  ReviewService.add_review
   (녹음 → 채점)  → speechsuper.assess_pronunciation(ref_text, audio_url)
                 → 글자별 {char, score, grade(상/중/하)} + 종합 점수
                 → Review 행 저장(feedback JSON)
                 → Sentence.evaluation 을 '마지막 시도' 점수로 덮어씀
```

실제 코드:
- 북마크: [domains/learning/service/sentence_service.py:27](../../domains/learning/service/sentence_service.py#L27)
- 복습 추가: [domains/learning/service/review_service.py:31](../../domains/learning/service/review_service.py#L31)
- Evaluation 덮어쓰기: [domains/learning/service/review_service.py:105](../../domains/learning/service/review_service.py#L105)
- 발음 채점 어댑터: [core/speechsuper.py:68](../../core/speechsuper.py#L68)

### SpeechSuper 채점

한국어 문장(`ref_text`)과 녹음(`audio_url`)을 SpeechSuper API 로 보내면 글자(음절)별 점수가 옵니다. 응답의 `words[]` 는 **항목 하나가 곧 글자 하나** 라서, 그대로 펼치면 글자별 상/중/하가 정확히 나와요(85+ 상, 70+ 중, 그 밑 하). 실제 코드: [core/speechsuper.py:299](../../core/speechsuper.py#L299) (`_map_char_scores`), [core/speechsuper.py:59](../../core/speechsuper.py#L59) (`_grade`)

### 키가 없으면? — 결정적 stub

SpeechSuper 키가 없거나 호출이 실패하면 **예외를 던지지 않고** 결정적 스텁으로 폴백합니다. 글자마다 `ord(글자)` 기반으로 항상 같은 점수를 만들어내는 가짜 채점이에요. 덕분에 키 없이도 UI/흐름을 그대로 테스트할 수 있습니다. 실제 코드: [core/speechsuper.py:336](../../core/speechsuper.py#L336) (자세한 graceful degradation 철학은 9장)

### 흔한 함정

복습 녹음의 `voice_url` 은 비공개 버킷의 **object key** 로 저장됩니다. 채점하러 SpeechSuper 가 그 파일을 가져오려면 임시 **signed URL** 이 필요해요. 코드는 채점용으로만 signed URL 을 만들고 DB 에는 key 를 그대로 둡니다. 실제 코드: [domains/learning/service/review_service.py:95](../../domains/learning/service/review_service.py#L95) (버킷/URL 규약은 9장)

> 한 줄 요약: 북마크한 표현을 사용자가 녹음하면 SpeechSuper 가 글자별 상/중/하로 채점하고, 그 결과가 Sentence 의 Evaluation 을 최신 점수로 갱신합니다(키 없으면 결정적 stub).

---

## 8.8 엔드포인트 한눈에

WebSocket 1개 + 통화/문장 REST 로 구성됩니다(모두 `/api/v1` 접두어, WS 제외 전부 인증 필요).

| METHOD | 전체 경로 | 함수 | 파일:줄 | 용도 |
|---|---|---|---|---|
| **WS** | `/api/v1/calls/stream?token=` | `ws_call_stream` | [ws_router.py:35](../../domains/learning/realtime/ws_router.py#L35) | 실시간 음성 통화 브리지 |
| GET | `/api/v1/calls/{id}/status` | `get_call_status` | [ws_router.py:79](../../domains/learning/realtime/ws_router.py#L79) | 분석 진행상태 폴링 |
| POST | `/api/v1/calls` | `create_call` | [call.py:21](../../domains/learning/routers/call.py#L21) | 통화 저장(배치) |
| GET | `/api/v1/calls` | `list_calls` | [call.py:26](../../domains/learning/routers/call.py#L26) | 내 통화 목록 |
| GET | `/api/v1/calls/{id}` | `get_call` | [call.py:33](../../domains/learning/routers/call.py#L33) | 통화 상세 |
| GET | `/api/v1/calls/{id}/result` | `get_call_result` | [call.py:38](../../domains/learning/routers/call.py#L38) | 결과 화면(평균+문장) |
| GET | `/api/v1/calls/{id}/raw` | `get_call_raw` | [call.py:44](../../domains/learning/routers/call.py#L44) | 턴별 원본 |
| PATCH | `/api/v1/calls/{id}` | `update_rating` | [call.py:49](../../domains/learning/routers/call.py#L49) | 만족도 갱신 |
| DELETE | `/api/v1/calls/{id}` | `delete_call` | [call.py:56](../../domains/learning/routers/call.py#L56) | 통화 삭제 |
| PATCH | `/api/v1/sentences/{id}/bookmark` | `set_bookmark` | [sentence.py:17](../../domains/learning/routers/sentence.py#L17) | 표현 북마크 |
| GET | `/api/v1/members/me/bookmarks` | `my_bookmarks` | [sentence.py:24](../../domains/learning/routers/sentence.py#L24) | 내 북마크 목록 |
| DELETE | `/api/v1/sentences/{id}` | `delete_sentence` | [sentence.py:29](../../domains/learning/routers/sentence.py#L29) | 표현 소프트 삭제 |
| POST | `/api/v1/sentences/{id}/tts` | `synthesize_sentence_tts` | [sentence.py:35](../../domains/learning/routers/sentence.py#L35) | 단건 온디맨드 TTS |
| POST | `/api/v1/sentences/{id}/reviews` | `add_review` | [sentence.py:47](../../domains/learning/routers/sentence.py#L47) | 복습 채점(key 기반) |
| POST | `/api/v1/sentences/{id}/reviews/audio` | `add_review_audio` | [sentence.py:59](../../domains/learning/routers/sentence.py#L59) | 복습 채점(파일 업로드) |
| GET | `/api/v1/sentences/{id}/reviews` | `list_reviews` | [sentence.py:81](../../domains/learning/routers/sentence.py#L81) | 복습 목록 |
| GET | `/api/v1/reviews/{id}/feedback` | `get_review_feedback` | [sentence.py:86](../../domains/learning/routers/sentence.py#L86) | 복습 채점 결과 |

라우터 등록/접두어: [main.py:164](../../main.py#L164), 접두어 상수: [main.py:35](../../main.py#L35)

---

## 8.9 관련 파일 지도

| 역할 | 파일 |
|---|---|
| WS 진입/인증 | [domains/learning/realtime/ws_router.py](../../domains/learning/realtime/ws_router.py) |
| 통화 본체(4펌프) | [domains/learning/realtime/call_session.py](../../domains/learning/realtime/call_session.py) |
| WS 메시지 프로토콜 | [domains/learning/realtime/protocol.py](../../domains/learning/realtime/protocol.py) |
| 통화 DB I/O + 분석 | [domains/learning/service/normalcall_service.py](../../domains/learning/service/normalcall_service.py) |
| 통화 CRUD/결과 | [domains/learning/service/call_service.py](../../domains/learning/service/call_service.py) |
| 복습/발음 채점 | [domains/learning/service/review_service.py](../../domains/learning/service/review_service.py) |
| 북마크/단건 TTS | [domains/learning/service/sentence_service.py](../../domains/learning/service/sentence_service.py) |
| 도메인 모델 | [domains/learning/models/](../../domains/learning/models/) |
| 프롬프트 조립 | [core/persona_prompt.py](../../core/persona_prompt.py) |
| Gemini Live 세션 | [core/gemini_live.py](../../core/gemini_live.py) |
| 구조화 분석 호출 | [core/gemini_analysis.py](../../core/gemini_analysis.py) |
| 표현 TTS | [core/tts.py](../../core/tts.py) |
| 오디오 포맷 | [core/audio.py](../../core/audio.py) |
| 발음 채점 | [core/speechsuper.py](../../core/speechsuper.py) |

---

## ✍️ 스스로 점검

1. 실시간 통화는 `asyncio.TaskGroup` 안에서 4개의 펌프를 동시에 돌립니다. 각 펌프의 이름과 역할을 하나씩 말해 보세요. 그리고 "5분이 지났을 때 실제로 종료 시드를 주입하는" 일을 시계(③)가 아니라 스피커 펌프(②)가 맡는 이유는 무엇인가요?
2. `load_call_setup` 은 왜 `Member` ORM 객체를 그대로 반환하지 않고 평범한 dict 로 변환해서 넘기나요? 만약 ORM 객체를 넘기면 async 통화 코드에서 무슨 일이 벌어질까요?
3. 통화후 분석에서 표현(Sentence)을 만들 때 점수가 아직 없는데도 빈 `Evaluation()` 을 붙여둡니다. 이 placeholder 점수는 나중에 어느 흐름의 어느 함수에서 채워지나요?

⟵ [이전: 인증 — Supabase Auth 위임](03-auth-supabase.md) ・ [📚 목차](README.md) ・ [다음: 외부 연동과 스토리지 — graceful degradation](09-external-and-storage.md) ⟶
