# 원가 계기판 3단계 — 엔진 구분(`usage_engine`) + `usage_peak_prompt` 의미 수정

작성 2026-08-07 00:28 · 브랜치 `feat/call-15min-polish` · 지시 bt-back(사장님 승인)

## 0. 왜 지금인가 — 캐스케이드 배포 전에 못 넣으면 영영 못 넣는다

어제 15분 실측(call 909, 902초)으로 **Live 15분 기준선 $1.8724**(분당 $0.1246)가 잡혔다.
그런데 지금 `call` 의 usage 8컬럼에는 **어느 엔진이 쓴 토큰인지가 없다**(`grep engine|provider|vendor` → 0건).
캐스케이드가 이 상태로 실사용에 들어가면 `SELECT AVG(...) FROM call` 에 Live 와 캐스케이드가
섞이고, **"캐스케이드가 정말 싼가"를 데이터로 증명할 수 없게 된다** — 그게 캐스케이드 프로젝트의
유일한 목적인데. 게다가 되짚을 수단도 없다: 그 행들이 어느 엔진이었는지 기록이 아예 없으므로
나중에 백필할 근거가 남지 않는다. **컬럼 1개의 시한은 캐스케이드 첫 배포다.**

## 1. 계약 (bt-back 확정 — cascade-impl 에도 동일 배포. 임의 변경 금지)

```
call.usage_engine  TEXT NULL
  '<모드>:<구성요소를 + 로 연결>'
  'live:gemini-native-audio'
  'live:openai-realtime'
  'cascade:google-stt-v2+gemini-2.5-flash+cloud-tts-chirp3-hd'
  'cascade:whisper+gemini-2.5-flash+cloud-tts-chirp3-hd'
```

토큰 4컬럼의 의미를 **엔진마다 고정**한다:

| 엔진 | in_audio / out_audio | in_text / out_text |
| --- | --- | --- |
| `live:*` | Live 모달리티 그대로 | Live 모달리티 그대로 |
| `cascade:*` | **0** (캐스케이드 LLM 은 오디오를 안 받는다) | **LLM 토큰** |

STT·TTS 는 단위가 토큰이 아니라 초·문자다. **컬럼에 섞지 않고** `usage_json.vendors` 에 둔다:

```json
"vendors": {
  "stt": {"vendor": "google-stt-v2",       "audio_s": 902.4},
  "llm": {"vendor": "gemini-2.5-flash",    "in_text": 41000, "out_text": 3200},
  "tts": {"vendor": "cloud-tts-chirp3-hd", "chars": 8400}
}
```

⛔ 달러 컬럼은 여전히 만들지 않는다(`models/call.py:89` 의 이유가 그대로 유효 — 단가는 벤더가
바꾸고, 토큰은 사실이며 원가는 파생 계산이다).

### 1-1. 계약에 대한 이견 — 없다. 다만 **함정 1개**를 코드로 막는다

계약 자체는 그대로 간다. 하지만 "같은 컬럼, 엔진마다 다른 의미"는 **원가 산식에 조용한 오류**를
심는다: 지금의 `estimate_usage_cost_usd` 는 `in_text` 에 Live 텍스트 단가 **$0.50/1M** 을 곱한다.
캐스케이드 행의 `in_text` 는 gemini-2.5-flash 토큰이라 실제 단가는 **$0.30/1M** 이다. 즉
캐스케이드 행을 기존 함수에 그대로 넣으면 **틀린 값이 조용히 나온다** — 게다가 그 값이
"캐스케이드가 싸다/비싸다"의 근거로 쓰인다.

→ 계약은 안 바꾸고, **엔진을 받는 상위 산식**을 하나 세운다.
`estimate_call_cost_usd(engine, ...)` 이 접두사로 갈라 Live 는 기존 표, cascade 는 벤더 단가표로
계산한다. 기존 `estimate_usage_cost_usd` 는 **Live 전용**임을 이름·주석·시그니처로 못박는다.

## 2. 단가표 — 조사 결과(2026-08-07 기준, 근거 URL 포함)

⚠ 공식 가격 페이지는 표가 JS 로 그려져 본문 추출이 안 됐다(`cloud.google.com/*/pricing` 직접
fetch → 헤더만 반환). 아래는 검색 결과 요약 기준이며, **과금 판단 전에 콘솔 청구서로 재확인**해야
한다. 코드 상수 주석에도 같은 경고를 남긴다.

| 구성요소 | 단가 | 근거 |
| --- | --- | --- |
| `google-stt-v2` (스트리밍/실시간) | **$0.016 / 분** (대량 시 최저 $0.004) | Google Cloud STT V2 실시간 인식 표준가 |
| `openai-whisper` (whisper-1) | **$0.006 / 분** | OpenAI 전사 API 정가, 볼륨 할인 없음 |
| `gpt-4o-mini-transcribe` | **$0.003 / 분** | 위와 같은 출처(참고용으로만 수록) |
| `cloud-tts-chirp3-hd` | **$30 / 1M 문자** (월 1M 문자 무료) | Google Cloud TTS Chirp 3 HD |
| `cloud-tts-neural2` / `wavenet` | **$16 / 1M 문자** | TTS 음성 등급표 |
| `cloud-tts-standard` | **$4 / 1M 문자** | 〃 |
| `gemini-2.5-flash` | 입력 **$0.30**, 출력 **$2.50** / 1M tok | Gemini API 가격표 |
| `gemini-2.5-flash-lite` | 입력 **$0.10**, 출력 **$0.40** / 1M tok | 〃 (2026-10-16 은퇴 예고) |

기존 Live 단가(변경 없음): 입력오디오 $3.00 / 입력텍스트 $0.50 / 출력오디오 $12.00 / 출력텍스트 $2.00 (per 1M tok).

### 2-1. 이 표로 본 캐스케이드 개략 — **구현 전에 알아둘 것**

call 909 과 같은 조건(902초, 사용자·비버 발화 반반, TTS 문자 ≈ 8,400)을 가정하면:

```
STT  902s × $0.016/60s                       = $0.241
LLM  in 41,000 × $0.30/1M + out 3,200 × $2.50/1M = $0.020
TTS  8,400자 × $30/1M                        = $0.252
합계                                          ≈ $0.51   (Live $1.87 의 27%)
```

⚠ 이건 **가정 3개**(LLM 입력 누적량·TTS 문자수·STT 과금이 통화 전 구간) 위의 개략이지 예측이
아니다. 실제 값은 캐스케이드가 이 컬럼을 채우는 순간 **측정된다** — 그게 이 작업의 목적이다.

## 3. `usage_peak_prompt` 는 지금 틀린 값을 담고 있다 (②)

`call_session.py:674` 가 압축을 감지할 때마다 `state.usage_prompt_peak = p` 로 **바닥값을 리셋**한다.
그래서 DB 에 남는 값은 "마지막 압축 사이클의 최고치"이지 **통화 최대치가 아니다.**

call 909 실증: DB **13,355** vs 로그 시계열 실제 최대 **15,904**(t=775.7s).

`models/call.py:114` 주석은 이 컬럼을 "이 통화가 도달한 최대 컨텍스트 — 압축·트리거 튜닝의
핵심 지표"라 적어놨다. **지금 그 용도로 못 쓴다.**

**왜 고치나 — 원가가 아니라 레버 때문이다.** 이 버그의 금전 손실은 0원이다(보고용 숫자일 뿐).
다음 단계인 **압축 트리거 16k→12k 실험**(−25%, 실측 기반 약 $0.47/통화)을 튜닝하려면
"이 통화가 실제로 몇 토큰까지 갔나"를 봐야 하는데, 지금 그 숫자가 틀려 있다.

### 수정 방침 — 관측값을 하나 **더** 세운다(기존 동작은 건드리지 않는다)

압축 감지는 **사이클 peak 가 있어야 성립**한다(톱니의 낙차를 재는 기준선이므로). 재접지 arm
게이트(`call_session.py:2500`)도 사이클 peak 를 읽어야 옳다 — "지금 컨텍스트가 트리거에
가까운가"가 질문이기 때문이다. 그러니 **리셋되는 사이클 peak 는 그대로 두고**, 통화 전체
최대치(`usage_prompt_max`, 절대 리셋 없음)를 나란히 세운 뒤 **DB 에는 후자를** 보낸다.

- `usage_prompt_peak` (기존, 리셋됨) → 압축 감지·재접지 arm 용. **동작 무변경.**
- `usage_prompt_max` (신규, 단조증가) → `summary["peak_prompt"]` → `call.usage_peak_prompt`.
- 사이클 peak 도 참고용으로 `usage_json.cycle_peak` 에 남긴다(둘이 갈라진 이유를 나중에 봐야 하므로).
- ⛔ 로그 줄 형식은 그대로다(`call_session.py:769` — 로그 기반 메트릭이 이 줄을 파싱 중).
  애초에 요약 로그 줄에 `peak_prompt` 는 안 찍힌다 → **바이트 무변경이 자동 보장**된다.
- `models/call.py` 주석을 실제 의미(통화 전체 최대·압축과 무관하게 단조)로 고친다.
- ⚠ **백필은 불가능하다.** 이미 쌓인 행의 `usage_peak_prompt` 는 사이클 peak 이고, 원본
  시계열은 Cloud Logging 에만 있는데 보존이 30일이다. 트리거 튜닝 표본은 **이 배포 이후
  통화**로 잡아야 한다 — 옛 행을 섞으면 과소평가된 peak 이 분포를 끌어내린다.

## 4. 작업 목록

| # | 파일 | 내용 |
| --- | --- | --- |
| 1 | `domains/learning/models/call.py` | `usage_engine` TEXT NULL 추가 + `usage_peak_prompt` 주석 수정 |
| 2 | `alembic/versions/c8f3a2b1d704_call_usage_engine.py` | `add_column` 1개 + `usage_peak_prompt` 주석 정정(`alter_column`, 데이터 무변경) / downgrade 채움. ⛔ **upgrade head 실행 금지** |
| 3 | `domains/learning/service/normalcall_service.py` | 엔진 상수·`build_engine_tag`·STT/TTS/LLM 단가표·`estimate_cascade_cost_usd`·`estimate_call_cost_usd`, `save_call_usage(engine=)` + `vendors` 저장 |
| 4 | `domains/learning/realtime/call_session.py` | `usage_prompt_max` 신설·요약 반영, Live 경로가 `ENGINE_LIVE_GEMINI` 를 넘김 |
| 5 | `tests/test_normalcall_ws.py` | 회귀 테스트(아래) |

### 회귀 테스트

- Live 경로가 `usage_engine='live:gemini-native-audio'` 를 **반드시** 남긴다
- `build_engine_tag` 가 계약 문자열을 정확히 만든다(빈 구성요소 무시)
- 캐스케이드 요약(vendors 포함)을 저장하면 컬럼 규약(오디오 0·텍스트=LLM)과 `usage_json.vendors` 가 그대로 남는다
- `estimate_call_cost_usd` 가 **엔진에 따라 다른 단가**를 쓴다(같은 토큰 수 → 다른 원가)
- 모르는 벤더는 조용히 0 으로 먹지 않고 **미상 목록으로 드러난다**
- 압축이 여러 번 나도 `peak_prompt` 가 **통화 전체 최대치**다(call 909 재현: 15,904 vs 13,355)
- 압축 감지·재접지 arm 동작은 **무변경**(사이클 peak 를 계속 쓴다)
- 요약 로그 줄 형식 무변경

## 5. 규율

- **R1** 이 문서. **R2** 모델+마이그레이션 같은 커밋. **R3** 쓰기는 service 가 명시 커밋.
- **R5** 계기판 실패가 통화를 죽이지 않는다(기존 `_persist_usage` 의 예외 흡수 유지).
- **R6** ⛔ `alembic upgrade head` 실행 금지 · ⛔ 배포 금지 · ⛔ 푸시 금지. 파일 작성까지만.
  DB 적용은 사장님 재확인 — demo/test/prod 가 **같은 Supabase**(`ppllscbfdvebsmdatpnc`)를 쓴다.

## 5-A. 추가 (2026-08-07, bt-back 판정) — 사고 토큰은 출력 원가다

cascade-impl 지적 채택. **gemini-2.5-flash 는 사고(thinking) 토큰을 출력 단가로 과금하는데
그 토큰은 응답 본문(candidates)에 안 들어온다** → `out_text` 만 세면 캐스케이드 LLM 출력
원가가 과소 계상된다. 값은 `usage_json.vendors.llm.thoughts` 로 이미 들어온다(컬럼 무변경).

- `estimate_cascade_cost_usd` 가 **`out_text + thoughts`** 로 계산한다. 왜 더하는지는 산식
  바로 옆 주석에 남겼다 — 안 그러면 다음 사람이 정리한답시고 뺀다.
- 게이트도 함께 고쳤다. 기존 `if` 가 `in_text/out_text` 만 봐서 **사고 토큰만 오고 `out_text` 가
  0 인 응답이 통째로 빠질 뻔했다**(원가 0 원으로 계상).

회귀 테스트: 사고 토큰이 있으면 캐스케이드 출력 원가가 정확히 `thoughts × 출력단가` 만큼
커진다(손계산 대조 + 사고 토큰만 온 경우 포함).

## 6. 보류(손대지 않음)

세션 스왑이 압축을 되돌리는 건(480.5s 11,729 → 500.3s 15,728 → 516.9s 11,643). 초과 3,628 tok ×
혼합단가 ≈ **$0.007**(통화 원가의 0.4%)이고 압축이 16초 만에 스스로 재교정한다. 새는 구멍이 아니라
일시적 튐 — bt-back 판단대로 **손대지 않는다.**
