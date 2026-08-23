# 실행 계획: 지시문을 setup 밖으로 빼고 통화 중 주입해 2.5 Live 에서 표정(set_face)을 살린다

- **작성일**: 2026-08-23
- **상태**: 계획 확정 / 미구현
- **브랜치**: `feat/call-15min` 에서 분기 예정 (`feat/live-persona-split`)
- **관련 파일**: `core/persona_prompt.py` · `core/gemini_live.py` · `core/config.py` ·
  `domains/learning/realtime/call_session.py` · `tests/test_normalcall_ws.py` ·
  `tests/test_persona_prompt.py` · `scripts/dev_dump_prompt.py`

---

## 0. 왜 이걸 하는가 — 실측 근거

`gemini-2.5-flash-native-audio-preview-09-2025`(AI Studio)에서 **긴 system_instruction 과
function tool 이 같은 setup 페이로드에 함께 있으면 통화가 100% 죽는다**(1011 Internal error,
`receive()` 0번째 메시지).

전부 **같은 시간대 라운드로빈**으로 잰 값이다(시간대 흔들림을 대조군으로 정규화).

| 조합 | 생존 | 표정 |
|---|---|---|
| 지시문 5,057자 + `SET_FACE_TOOL` **setup 에 전부** (현행) | **0/8** | 0/8 |
| 지시문 46자 setup + 툴 → 붙은 뒤 나머지를 통화 중 주입 | **14/14** | 14/14 |
| 지시문 46자 + 툴만 (주입 없음) | 100% | 100% |
| 긴 지시문 + 툴 **없음** | 7/8 | — |
| 지시문을 선톡 시드(첫 메시지)에 넣기 | 1/6 | — |

**⇒ 위치가 아니라 총량이 문제다.** setup 이 가벼우면 붙고, 붙은 뒤에는 5,057자를 통째로
밀어넣어도 안 죽는다. 무음 턴 0/70, 규칙 유출 0건.

길이 경사(툴 고정, 라운드로빈):

```
69자  7/7    1,172자 1/7    1,969자 3/7    2,697자 0/7    3,695자 0/7
```

### 이미 반증된 가설 (되풀이 금지)

```
tool 이 필요조건이다              ⛔ 툴 없이도 1011 이 난다(_e2e 2/6)
한국어 설명이 죽인다              ⛔ 한글 600B 무인자 툴은 8/8
setup 총 바이트 임계다            ⛔ 영문 11KB 설명(setup 19,412B)이 8/8
함수 이름(set_face)이 죽인다      ⛔ 이름 6종 교체해도 0/7
thinking_budget=0                ⛔ 0/6
scheduling=SILENT · 압축 8000/7000 ⛔ 운영 리비전 복제해도 0/7
history_config                   ⛔ AI Studio 가 1007 로 거부
지시문을 영어로                   ⛔ 설계 오류로 낸 오판(범인을 상수로 박아두고 쟀다)
```

### 벤더 쪽 근거

`googleapis/python-genai#1832` — *"[Fails on AI Studio] Function calling produces internal
error in gemini-2.5-flash-native-audio-preview-09-2025"*. 2025-12-08 개설, **여전히 open**,
담당자 배정됐으나 답변 없음, 워크어라운드 없음. **우리 모델·조합·백엔드가 그대로 적힌
티켓이다.** 우리 코드 문제가 아니다.

---

## 1. 목표 & 범위

### 목표
표정 tool 을 켠 채로 **일반 통화가 정상적으로 붙고 끝까지 간다.**

### MVP 범위
- `system_instruction` 을 (setup 코어 / 통화중 주입 조각) 으로 나눈다
- 인사 턴이 끝난 뒤 조각을 `turn_complete=False` 로 적재한다
- env 스위치 뒤에 둔다(`LIVE_PERSONA_INJECT`, 기본 off → 종전과 바이트 동일)
- 별건 1줄: `LIVE_FACE_SPIKE` 가 레벨테스트에 툴을 붙이는 것을 막는다

### 비범위
- 모델 교체(3.1) — 별도 판단. 원가 +15%, 인사 톤 튜닝 필요
- 재개(session resumption)로 툴을 나중에 붙이는 길 — 조사 중, 되면 후속
- 재연결/재시도 정책 — 별건
- 프론트 변경 — **없다**(마커 형식 동일)
- 캐스케이드 — 무관

---

## 2. 아키텍처 & 데이터 흐름

### 지금

```
run_call
  build_system_instruction(...)          → 3,700~5,980자
  open_session(system_instruction=전체, tools=[SET_FACE_TOOL])
                                          ⛔ setup 이 무거워 1011
  send_text_turn(seed_opening)            선톡 234자
```

### 바꿀 것

```
run_call
  full = build_system_instruction(...)              ⛔ 이 함수는 손대지 않는다
  core, chunks = split_persona_for_live(full)       ⭐ 신규 — 출력을 소비하는 후처리
  open_session(system_instruction=core, tools=[SET_FACE_TOOL])
  state.persona_parts = chunks
  send_text_turn(seed_opening)                      선톡 (그대로)

_pump_gemini_to_client · turn_end 블록 (:2567)
  _flush_beaver_segment(state)            ← beaver_turns += 1 (:724)
  if   state.close_seed_sent:   ...
  elif state.should_close:      _inject_close_seed(...)
  elif state.tag_leak_seen:     _inject_resume_seed(...)
  elif state.persona_parts:     _inject_persona_part(...)   ⭐ elif 맨 끝
```

`turn_end` 직후는 **비버 idle** 이라 마이크가 이미 열려 있다(`state.turn_id is None`, `:2316`).
`turn_complete=False` 로 적재하면 생성이 안 일어나고(이중발화 0), 학습자가 말하면 VAD 가
턴을 닫으며 그 생성에 페르소나가 함께 실린다.

⭐ 이건 내 하네스가 실제로 잰 그 자리다 — 하네스도 학습자 음성을 밀어넣기 **전**에 주입했다.

### 분할 방식 — ⛔ `build_system_instruction` 을 고치지 마라

`tests/test_persona_prompt.py` 의 바이트 스냅샷 3건(`:187` `:197` `:561`)이 출력을 얼려
두고 있다. 분할은 **출력을 받아서 자르는 순수 함수**로 만든다.

```python
# core/persona_prompt.py 신규
def split_persona_for_live(full: str) -> tuple[str, list[str]]:
    """(setup_core, in_call_chunks). 순수 문자열 조작 — LLM 생성 0."""
```

불변식(회귀로 고정): `core + "".join(chunks)` 가 원본과 **바이트 동일**.

### 실측한 지시문 구성

```
[0]   69자  정체성
[1]   29자  모국어
[2]  277자  페르소나(role/personality)
[3] 2513자  불변 규칙          ← 전체의 68%. 종료 규약·code-switching 이 여기
[4]   27자  학습자 수준
[5]   18자  학습자 흥미
[6]  750자  표정 블록
        (+ 항목 30개 주입 시 약 5,057자, full 케이스 5,980자)
```

### setup 코어에 무엇을 남기나

⛔ **46자로 가지 마라.** 압축은 `system_instruction` 만 면제하고 대화 히스토리는
오래된 것부터 밀어낸다. 주입된 페르소나는 턴1~2에 놓이므로 **압축의 1순위 희생자**다.
종료 규약이 조각으로 내려가면 압축 후 **비버가 먼저 작별하는 사고**(과거 8건 기록,
call 706 47초 死구간 / call 870 4분24초 자체종료)가 재발한다.

⚠ 운영 압축 설정은 **8000/7000**(`gcloud run revisions describe` 로 확인. 코드 기본값
16000/12000 과 다르다). 그리고 코드 주석에 **5분 통화에서 압축이 실제로 돌았다는 실측**이
박혀 있다(`call_session.py:193-197`, call 1045: 7,659 → 7,165). **5분도 닿는다.**

⇒ 코어에 남길 것(잃으면 사고 나는 것만):

```
1. 정체성 1줄
2. 모국어 + 언어 정책 1줄
3. 종료 규약 — "종료는 서버가 정한다. 먼저 작별하지 마라"      ⛔ 반드시 코어
4. 대괄호 안내문 낭독 금지                                    ⛔ 반드시 코어
5. 응답 길이 + 질문 뒤 대답 기다려라                          자문자답하면 학습자 턴이
                                                            안 생겨 주입 자리가 사라진다
```

### setup 예산 — 실측 확정

라운드로빈 9바퀴(선톡 시드 234자 동반, 툴 현행 고정):

```
코어    총량(코어+시드)   생존        표정
 46자      280자         8/9  88.9%    8/9
158자      392자         8/9  88.9%    8/9
380자      614자         9/9 100.0%    9/9   ⭐
494자      728자         6/9  66.7%    6/9
788자     1022자         2/9  22.2%    2/9
993자     1227자         6/8  75.0%    6/8
```

⇒ **코어 예산 = 380자**(선톡 시드 포함 총 614자). 500자를 넘으면 급격히 무너진다.
788자가 993자보다 나쁜 것은 벤더 흔들림이고, **≤400자 구간은 9바퀴 내내 안정적**이다.

⚠ 상한을 코드 상수로 박고(`LIVE_SETUP_MAX_CHARS = 380`) 초과 시 **경고만** 낸다
(⛔ `raise` 금지 — R5). 실패는 회귀 테스트가 낸다.

⭐ 380자면 위 5줄이 다 들어간다(초안 실측 ~294자, 여유 ~85자).
⛔ `role`/`personality` 는 코어에 넣지 마라 — DB 자유 텍스트라 **예산이 비결정적**이 된다
(트래시토커 페르소나 하나가 300자를 먹을 수 있다). 캐릭터는 첫 조각과 함께 도착한다.

---

## 3. 작업 분해

**A. 별건 선행 (분할과 무관, 지금 위험)**
- [ ] A1 — `call_session.py:1346` 의 `LIVE_FACE_SPIKE` 가 `call_type` 분기 **밖**에 있어
  레벨테스트에도 툴을 붙인다. 레벨테스트 지시문은 2,111자로 0/7 구간 ⇒ 스위치를 켜는 순간
  레벨테스트가 죽는다. `and call_type != "level_test"` 1줄.

**B. 분할 (순서대로)**
- [ ] B1 — `split_persona_for_live()` 신규 (`core/persona_prompt.py`). 바이트 왕복 보장
- [ ] B2 — `LIVE_PERSONA_INJECT` env 스위치 (`core/config.py`, 기본 `False`)
- [ ] B3 — `LiveSession.send_persona(text)` 신규 (`core/gemini_live.py`).
  ⛔ `send_reground` 를 재사용하지 마라 — 재접지 관측 채널이 오염돼 기존 시험 3건이
  의미를 잃는다. 호출부는 `AttributeError` 를 삼켜 구형 fake 와 공존(R5)
- [ ] B4 — `LiveSessionProtocol`(`:190`) 시그니처 정정. 지금 `send_reground` 선언이
  실제 구현과 이미 어긋나 있다(`turn_complete` kwarg 누락)
- [ ] B5 — `_CallState` 필드 3개(`persona_parts` `persona_sent` `persona_fail`)
- [ ] B6 — `_inject_persona_part()` + `turn_end` elif 체인 맨 끝에 훅
- [ ] B7 — 조립부(`run_call:1179`)에서 분할 적용. 이어하기 브리프(`:1305`)의 거처 결정
- [ ] B8 — 가드: `_reground_due` 맨 앞 `if state.persona_parts: return ""`,
  `_inject_close_seed` 진입 시 `state.persona_parts.clear()`
- [ ] B9 — 종료 요약 로그 1줄(`normalcall 페르소나: setup=N자 조각=k/n 주입완료`)
- [ ] B10 — `scripts/dev_dump_prompt.py` 동반 수정(코어/조각을 나눠 찍는다)

**C. 회귀** (B와 병렬 가능)
- [ ] C1 — 깨진 기존 시험 복구(§4)
- [ ] C2 — 신규 회귀 10종(§4)

---

## 4. 수용 기준 & 테스트 포인트

### 기준선 (실측)

```
사정권 6개 파일   261 passed, 1 skipped   기존 실패 0건
   test_normalcall_ws · test_gemini_live_config · test_live_face_spike
   test_persona_prompt · test_level_test_call · test_hint_teaching
⇒ 변경 후 이 6개에서 나는 실패는 전부 새 실패다
⚠ test_cascade_* 7~8건은 원래 실패 — 변경 전후 목록이 같아야 한다
```

### 깨질 기존 시험

| 파일:테스트 | 이유 |
|---|---|
| `test_normalcall_ws.py:714 test_reground_on_user_turn_attaches_once` | `len(fake.regrounds)==1` — 주입이 같은 채널이면 오염. **B3(별도 채널)로 회피** |
| `test_normalcall_ws.py:743 test_reground_skipped_near_close` | `regrounds == []` — 동상 |
| `test_normalcall_ws.py:838 test_late_reminder_actually_attaches` | 조각만으로 통과해 시험이 죽는다 |
| `test_level_test_call.py:326 test_member_with_level_routes_to_normal` | `"[학습자 수준]" in instr` — 레벨 프로파일이 조각으로 빠지면 즉사 |
| `test_normalcall_ws.py:2236/2257/2267` 언어 3종 | `"프랑스어" in si` — 코어가 언어를 유지하면 통과하되 **의미 축소** |
| `test_normalcall_ws.py:1711 test_tools_not_passed_for_either_call_type` | `LIVE_FACE_SPIKE` 를 켜면 실패 |
| 가짜 세션 16개 중 13개 | 새 메서드 미구현 → `AttributeError`. B3 의 삼킴으로 방어 |

### 신규 회귀 10종

```
1  setup 코어가 상한 이하 (로케일 6 × 밴드 4 × 캐릭터 최대길이 파라미터화)
2  core + chunks 가 원본과 바이트 동일 (스냅샷과 잇는 다리)
3  코어에 종료 태그 규약 + 낭독 금지가 있다
4  조각이 앞선 조각에 없는 표제를 참조하지 않는다
5  인사 턴에는 주입 0회
6  첫 조각이 인사 turn_end 직후, turn_complete=False, send_text_turn 으로 안 샌다
7  통화당 정확히 1세트 (더 안 늘어난다)
8  페르소나와 재접지가 카운터를 공유하지 않는다
9  should_close / close_seed_sent 면 주입 0 + 남은 조각 clear
10 주입 예외에도 통화가 정상 종료되고 다음 턴에 재시도(상한 2회)
   + 새 메서드가 없는 세션에서도 통화 완주
```

### 실통화 게이트 (순서대로, 앞이 막히면 뒤는 무의미)

```
G0  정적    사정권 6파일 261 passed 회복 + 신규 10종 통과
            ⛔ "삭제해서 통과"는 실패로 본다
G1  생존    14건 중 연결 성공 14/14 · 무음턴 0
            ⛔ 1011 이 1건이라도 나면 중단 (실측이 14/14 였으므로 그보다 낮으면 재현 실패)
G2  기능    표정 마커 14/14 · 정상 작별 ≥13/14 · 이중발화 0 · 대괄호 낭독 0
G3  품질    사람이 듣는다 5건. ⚠ 조각 도착 전 1~2턴을 특히 들어라
G4  장통화  15분 3건. 압축 1회 이상 발생 후에도 캐릭터·모드 유지
            ⛔ 여기서 깨지면 롤백이 아니라 설계 보강(압축 후 재주입)
G5  원가    usage 총 토큰이 기준 대비 ±15% 이내
```

**롤백 트리거**: G1 미달 · 이중발화 ≥2 · 대괄호 낭독 ≥2 → 즉시 스위치 off

---

## 5. 리스크 & 결정 사항

### 확정된 위험

| # | 위험 | 대응 |
|---|---|---|
| R1 | **압축이 주입된 페르소나를 밀어낸다.** 운영 8000/7000 이고 5분에도 압축이 돈다(실측). 주입분은 턴1~2라 1순위 희생자 | 종료 규약·언어 정책을 **코어에 남긴다**. 나머지 드리프트는 기존 재접지가 담당(이미 `post-compress` 트리거 존재) |
| R2 | **종료 시드와 겹치면 작별이 오염된다.** 무음 3단이면 82초에 종료 시드가 나가는데 그때 조각이 pending | ①elif 맨 끝 ②`_inject_close_seed` 진입 시 clear ③`CONTROL_TAG` 접두어로 낭독돼도 저장본 정화 |
| R3 | **재접지와 동시 발화.** 선제 arm 임계가 `trigger×0.85 = 6,800` 인데 실통화 턴당 prompt 가 6,000~9,500 ⇒ 초반에 걸린다 | `_reground_due` 맨 앞 가드 + 얹기 조건 가드 (두 겹) |
| R4 | **인사~2턴 캐릭터 공백** | 코어 5줄이 최소 방어. 사장님이 "인사 턴은 캐릭터 없어도 된다"고 결정 |
| R5 | **가짜 세션 13개가 새 메서드를 모른다** | 호출부에서 `AttributeError` 삼킴(R5) + 그걸 지키는 회귀 1종 |

### 미확인 — 실통화로만 답이 나오는 것

```
⚠ in-call 주입이 세션을 죽이는지는 미측정이다.
  하네스가 증명한 건 "setup 이 작으면 산다"이지 "in-call client_content 가 안전하다"가 아니다
⚠ turn_complete=False 가 오디오 VAD 턴과 병합되는지는 벤더 미보장(SDK docstring 명시)
⚠ 15분 통화에서 페르소나가 유지되는지 — 5분 14/14 는 이 구간을 한 번도 안 지났다
```

### 사장님 결정이 필요한 것

```
1. 이어하기 브리프 거처   코어(380자 예산 압박) vs 조각(늦게 도착)
                       ⇒ 브리프가 수백 자라 코어에 넣으면 예산을 뚫는다.
                          조각 맨 앞으로 보내는 쪽을 권한다
2. 압축 후 재주입 정책   G4 결과가 강제할 가능성이 높다
3. 캐릭터를 코어에 넣을 것인가  ⛔ 권하지 않는다(예산 비결정). 대신 조각1을
                            role/personality 로 시작해 가장 먼저 도착시킨다
```

**해결됨**: setup 코어 예산 = **380자**(실측 9/9). §2 참조.

### 가정

- 사장님 결정: **인사 턴은 캐릭터 없이 가도 된다**
- `session_epoch > 1` 은 죽은 경로(세대 루프 2026-08-19 제거). "재개"의 실체는 **이어하기
  조각2**이고, 조각마다 `run_call` 이 새로 도니 주입도 조각마다 다시 일어난다
- 프론트는 안 건드린다 — 마커 형식(`emotion="happy"`) 동일
