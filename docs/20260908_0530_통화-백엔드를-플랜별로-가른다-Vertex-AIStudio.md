# 통화 백엔드를 플랜별로 가른다 — Free·Pro=Vertex / Max=AI Studio

```
작성일   2026-09-08 05:30
상태     ⛔ 기획(미구현). 착수 전 §7 의 결정 4건과 §8 의 선행 실측 2건이 닫혀야 한다.
브랜치   미정 (현재 fix/live-model-name → dev 푸시 완료, c56edc7)
```

## 0. 한 줄

**같은 2.5 모델이 Vertex 에서는 1.15초, AI Studio 에서는 2.82초다.** 버전 문제도
우리 코드 문제도 아니고 **백엔드 문제**다. 그래서 Free·Pro 를 Vertex 로 되돌리고
Max 는 AI Studio 3.1 에 둔다. 클라이언트를 **하나 더 만들어 통화마다 고른다.**

---

## 1. 왜 — 실측 근거

### 1-1. 맨몸 A/B (설정은 `response_modalities=["AUDIO"]` 한 줄뿐)

지시문·도구·압축·전사·이력을 **전부 뺀** 세션에 실제 학습자 음성 10개를 20ms 씩
실시간 속도로 흘리고, **말이 실제로 끝난 시각**부터 오디오 200ms 가 쌓일 때까지를 쟀다.
파일마다 순서를 섞어 회선 편향을 상쇄했다(파일10 × 2회 × 4구성 = 80세션).

| 구성 | n | **중앙값** | p25~p75 | 최소~최대 | 실패 |
|---|---|---|---|---|---|
| **Vertex `gemini-live-2.5-flash-native-audio`** | 18 | **1.15초** | 0.96~1.26 | 0.10~1.42 | 2 |
| AI Studio `gemini-2.5-flash-native-audio-preview-09-2025` | 20 | 2.82초 | 2.38~3.20 | 1.93~3.63 | 0 |
| AI Studio `gemini-2.5-flash-native-audio-latest` | 20 | 2.95초 | 2.59~3.28 | 1.69~3.95 | 0 |
| AI Studio `gemini-3.1-flash-live-preview` | 20 | 1.30초 | 1.26~1.40 | 0.46~1.52 | 0 |

같은 파일끼리 짝지어도 흔들리지 않는다:

```
파일      Vertex2.5   AIS 3.1   AIS 2.5
u0004       0.32       0.47      2.15
u0006       0.12       0.57      2.41
u0012       0.97       1.40      2.98
u0018       1.37       1.50      3.44

Vertex2.5 가 3.1 보다 빠른 파일        8/9
Vertex2.5 가 AI Studio 2.5 보다 빠른   9/9   (360쌍 전부, 100%)
```

⭐ **AI Studio 2.5 의 최소값(1.93초) > Vertex 의 최대값(1.42초)** — 분포가 겹치지 않는다.

⚠ 이 수치는 **Vertex 에 불리한 조건**에서 나왔다. 측정 PC 는 한국, Vertex 는 us-central1,
AI Studio 는 글로벌 엔드포인트라 아시아로 붙었을 가능성이 크다. 왕복 핸디캡을 안고 이겼다.

### 1-2. 버전 문제가 아니다

끝무음을 잘라낸 하드컷 조건(파일10 × 2회):

```
AI Studio 2.5 -09-2025   2.18초
AI Studio 2.5 -12-2025   2.22초
AI Studio 2.5 -latest    2.19초      ⇒ 세 버전이 사실상 동일
AI Studio 3.1            0.77초
```

### 1-3. 우리 코드가 더하는 지연은 ≈ 0

```
맨몸 AI Studio 2.5   2.82초
실제 통화 30일치      2.51초  (응답지연 끝기준 중앙값, n=18)
```

지시문·도구·압축·전사·재접지를 다 얹은 실제 통화가 맨몸보다 **느리지 않다.**
⇒ 튜닝으로 줄일 여지가 없다. 백엔드를 바꾸는 것 말고는 방법이 없다.

### 1-4. AI Studio 2.5 의 다른 결함 둘 (30일치 로그 전수)

| | 2.5 | 3.1 |
|---|---|---|
| 루프 턴(오디오 ≥4초인데 초당 3자 미만) | **3 / 43 = 7.0%** | **0 / 277 = 0%** |
| 태운 오디오 | 46초 | 0초 |

루프 실례(call 1353 t11): 텍스트 `하! "저는` 6자에 **오디오 23.66초**. GCS 저장본
파형 분석 결과 **주기 0.28초에서 자기상관 r=0.87** — 같은 소리의 반복이 확정이다.
그리고 도착이 1.61배속이라 통화 종료 시점에 폰에 **미재생 9초**가 남아 "렉"으로 들렸다.

⛔ 그리고 **맨몸 AI Studio 2.5 세션 20판 중 1판(5%)이 `1011 Internal error`** 로 끊겼다.
도구·지시문이 없어도 난다.

---

## 2. 지금 구조 (코드가 근거)

### 2-1. 클라이언트는 **전역 하나**다

```
main.py:239   app.state.genai_client = _create_genai_client(settings)
main.py:142   if settings.USE_VERTEX:        ← 이 한 줄이 앱 전체를 가른다
main.py:172   return genai.Client(api_key=settings.GEMINI_API_KEY, **extra)
```

그 하나를 **Live · 캐스케이드 · 통화후 분석**이 공유한다:

```
domains/learning/realtime/ws_router.py:53     client = app.state.genai_client       (통화)
domains/learning/realtime/cascade_router.py:82,85                                    (캐스케이드)
domains/learning/routers/call.py:181,225,273                                         (분석·재분석)
core/gemini_analysis.py:112                   generate_structured(client, ...)
```

### 2-2. 그런데 **두 번째 클라이언트를 만드는 전례가 이미 있다** ⭐

```
main.py:243   app.state.cascade_llm_client, app.state.cascade_llm_location
                  = _create_cascade_client(settings, app.state.genai_client)
main.py:188   # ⇒ 교체가 아니라 **추가**다. `app.state.genai_client` 는 그대로 둔다.
```

캐스케이드 대답 LLM 은 **리전만 다른** 클라이언트를 따로 만들어 쓴다. 이 설계가
"객체를 하나 더 만들어 호출부가 고른다"를 이미 검증했다. **같은 모양으로 간다.**

### 2-3. 플랜 분기는 이미 있다

```
call_service.py:139   CALL_VIDEO_BY_PLAN   = {None: False, "pro": False, "max": True}
call_service.py:147   CALL_LIVE_MODEL_BY_PLAN = {None:"voice", "pro":"voice", "max":"video"}
call_service.py:162   live_model_for(db, member_id) -> str
call_session.py:1452  wants_video, live_model = await svc.run_db(...)   ← 한 번의 run_db 로 둘을 같이 읽는다
call_session.py:2190  factory_kwargs["model"] = state.live_model
```

⭐ ⛔ `config.py:116` 이 못박은 규율: **"값을 여기서 고르지 마라 — 고르는 곳은
`call_service.live_model_for()` 하나다. 두 곳에서 고르면 언젠가 갈라진다."**
백엔드 축이 늘어나도 **고르는 곳은 그 함수 하나**를 유지한다.

---

## 3. ⛔ 충돌 지점 — 전역 `USE_VERTEX` 를 읽는 자리 전수

`grep -rn "USE_VERTEX" --include=*.py` 로 뽑은 **실행 경로 3곳**이다. 클라이언트만
둘로 만들면 이 셋이 **틀린 쪽에 걸린다.**

### C1. `safety_settings` — ⛔ 최우선. 걸리면 세션이 안 열린다

```python
core/gemini_live.py:304
    **({"safety_settings": _LIVE_SAFETY} if settings.USE_VERTEX else {}),
```

코드 주석이 실측을 이미 적어 뒀다(`:295-303`):

> ⛔⛔ **AI Studio 는 setup 에서 safetySettings 를 안 받는다**(2026-08-20 실측).
> 실패 원문: `1007 Invalid JSON payload received. Unknown name "safetySettings"` →
> 통화가 **연결 즉시** 죽는다(msgs=0). (call 1115·1116)

⇒ 혼합 운영에서 전역 플래그가 `true` 면 **Max(AI Studio) 통화가 전부 1007 로 죽는다.**

### C2. `session_resumption.transparent` — 같은 함정

```python
core/gemini_live.py:265
    transparent=True if settings.USE_VERTEX else None,
```

Vertex 전용. AI Studio 에 넘기면 SDK 가 `ValueError`.
⚠ 지금은 `LIVE_SESSION_RESUMPTION=false`(app-api env)라 **잠들어 있다.** 켜는 순간 터진다.

### C3. `build_live_config` 가 백엔드를 알 방법이 없다 — ⛔ 구조적 문제

```python
core/gemini_live.py:218-225
def build_live_config(*, system_instruction, voice=DEFAULT_VOICE, tools=None,
                      resume_handle=None, input_language_codes=None) -> LiveConnectConfig
core/gemini_live.py:24
from core.config import Settings, settings      ← 모듈 전역 싱글턴을 직접 읽는다
```

인자로 백엔드를 못 받는다. **C1·C2 를 고치려면 이 시그니처를 바꿔야 한다.**

### C4. ⭐ 좋은 소식 — 자격증명 갱신은 이미 안전하다

```python
core/gemini_live.py:565-575  _ensure_fresh_credentials(client)
    creds = getattr(getattr(client, "_api_client", None), "_credentials", None)
    if creds is None or getattr(creds, "valid", False): return
```

**클라이언트 객체를 보고 판단**한다(전역 플래그가 아니다). api_key 클라이언트는
`_credentials` 가 없어 조용히 건너뛴다. **혼합 운영에서 그대로 옳다. 손댈 필요 없다.**

### C5. 캐스케이드·분석이 기본 클라이언트에서 파생된다

```
main.py:243   _create_cascade_client(settings, app.state.genai_client)
main.py:212   if not where or where == base:  → 기본 클라이언트를 **그대로 재사용**
```

기본을 Vertex 로 바꾸면 **캐스케이드 대답 LLM 과 통화후 분석(`JUDGE_MODEL`)도 같이
Vertex 로 간다.** 의도인지 아닌지를 §7-D1 에서 정한다.

### C6. 플랜 분기를 안 타는 경로가 둘 있다

```
call_session.py:1412  live_model: str | None = None      # 레벨테스트는 분기 밖
call_session.py:1417  wants_video: bool = False
gemini_live.py:627    live_model = model or settings.GEMINI_LIVE_MODEL
```

**레벨테스트**와 **캐스케이드**는 `live_model_for()` 를 안 부르고 `GEMINI_LIVE_MODEL`
폴백으로 떨어진다. 그 폴백 값은 지금 AI Studio 이름이다(`config.py:108`).
⇒ 기본 클라이언트를 Vertex 로 바꾸면 **그 둘이 「Vertex 클라이언트 + AI Studio 모델명」
조합이 되어 1008 로 죽는다.** ⛔ 이게 이번 설계에서 가장 조용히 터질 자리다.

### C7. 모델 이름이 백엔드마다 다르다 — 실측

```
Vertex 에서 열리는 것        gemini-live-2.5-flash-native-audio                 ✅
Vertex 에서 1007/1008        gemini-live-2.5-flash-preview-native-audio-09-2025 (1007)
                             gemini-live-2.5-flash-preview-native-audio         (1008)
                             gemini-2.5-flash-native-audio-preview-09-2025      (1008)
                             gemini-live-2.5-flash-preview                      (1008)
                             gemini-3.1-flash-live-preview                      (1008)  ⛔
AI Studio 에서 열리는 것     gemini-2.5-flash-native-audio-{latest,-09-2025,-12-2025}
                             gemini-3.1-flash-live-preview
AI Studio 에서 1008          gemini-live-2.5-flash-native-audio                 ⛔
```

⛔ **3.1 은 Vertex(us-central1)에 없다.** 그래서 "전부 Vertex" 는 선택지가 아니다 —
Max 의 영상통화를 포기해야 한다. **혼합이 필수다.**

⇒ 표는 이름만이 아니라 **(백엔드, 모델) 쌍**을 담아야 한다.

### C8. 원가 계산이 조용히 틀릴 수 있다

```
normalcall_service.py:1130
   ① 이중계상 위험이 클라이언트마다 다르다. AI Studio 는 candidates 에 사고 토큰이
      **포함**돼 나오고 Vertex 는 **빠진다**. 이 앱은 Vertex 지만(USE_VERTEX) …
```

이 주석은 **앱 전체가 Vertex 라는 전제**로 쓰였다. 혼합이 되면 전제가 깨진다.
다만 `build_engine_tag("live", model)` 이 모델명을 담고(`test_plan_call_split.py:182`),
백엔드마다 모델명이 다르므로 **행은 자연히 갈린다.** 단가표(`LIVE_TOKEN_PRICE_USD`)가
두 백엔드에서 같은지는 확인이 필요하다(§8-B).

### C9. 이 영역을 잠그는 회귀 시험 — 반드시 같이 고친다

```
tests/test_gemini_live_config.py:93   test_transparent_only_on_vertex
tests/test_gemini_live_config.py:139  test_safety_relaxes_only_harassment
tests/test_gemini_live_config.py:176  test_vertex_only_fields_are_omitted_on_the_api_key_path
tests/test_gemini_live_token.py        (5건 — creds 갱신)
tests/test_plan_call_split.py          (플랜 표·게이트·엔진 태그)
tests/test_live_model_name.py:121      ⚠ "AI Studio 기준이다(USE_VERTEX=false). Vertex 는 목록이 다르다"
```

셋 다 `monkeypatch.setattr(settings, "USE_VERTEX", ...)` 로 **전역 플래그를 흔들어** 잰다.
시그니처를 바꾸면 이 시험들이 깨진다 — **깨지는 게 정상이고, 새 계약으로 다시 못박는다.**

---

## 4. 설계

### 4-1. 축을 하나 늘린다 — `backend`

```
call_type   normal | level_test      무슨 통화인가          (기존)
engine      live | cascade           어느 배관으로          (기존)
call_mode   study | chat             대화 성격 · 서버 내부   (기존)
backend     vertex | studio          ⭐ 어느 구글 API 로     (신규)
```

### 4-2. 표를 (백엔드, 모델) 쌍으로

```python
# call_service.py — 고르는 곳은 여전히 **여기 하나**다
CALL_BACKEND_BY_PLAN: dict[str | None, str] = {
    None:  "vertex",   # Free — 빠르고 싸다
    "pro": "vertex",
    "max": "studio",   # Max  — 3.1 은 Vertex 에 없다(C7)
}

def live_backend_for(db, member_id) -> str: ...
def live_model_for(db, member_id) -> str:    # 기존 함수가 backend 를 같이 본다
```

⚠ `live_model_for` 는 이미 `settings.LIVE_MODEL_VOICE/VIDEO` 를 읽는다. 이름이
백엔드마다 다르므로 **설정 키를 백엔드별로 갈라야 한다**:

```
LIVE_MODEL_VOICE_VERTEX = gemini-live-2.5-flash-native-audio
LIVE_MODEL_VIDEO_STUDIO = gemini-3.1-flash-live-preview
```

⛔ 기존 `LIVE_MODEL_VOICE`/`LIVE_MODEL_VIDEO` 는 **폐기하지 않는다** — 새 키가 비면
그쪽으로 떨어져 **종전 동작 그대로**여야 한다(`config.py:114` 의 하위호환 규율).

### 4-3. 클라이언트 둘 — `_create_cascade_client` 와 같은 모양

```python
main.py
  _create_genai_client(settings, *, use_vertex: bool | None = None, location=None, http_options=None)
      # use_vertex=None 이면 settings.USE_VERTEX (⇒ 기존 호출부 바이트 동일)

  app.state.genai_client         = _create_genai_client(settings)              # 기본 = 종전 그대로
  app.state.genai_client_vertex  = _create_genai_client(settings, use_vertex=True)
  app.state.genai_client_studio  = _create_genai_client(settings, use_vertex=False)
```

⭐ **기본(`genai_client`)의 의미를 바꾸지 않는다.** 캐스케이드·분석·레벨테스트가 전부
그것을 보고 있으므로(C5·C6), 기본을 건드리면 통화와 무관한 것들이 같이 움직인다.
**추가만 한다** — `_create_cascade_client` 가 세운 규율 그대로다.

⚠ 둘 중 하나가 `None` 이면(키 부재 등) 그 플랜만 **기본 클라이언트로 떨어진다**(R5).
통화가 통째로 죽으면 안 된다.

### 4-4. 통화마다 고른다 — 고르는 자리는 `call_session` 안

⛔ `ws_router.py:53` 은 **member_id 를 알기 전에** 클라이언트를 꺼낸다. 거기선 못 고른다.

```
ws_router.py:53      client = app.state.genai_client        ← 그대로 둔다(폴백)
ws_router.py:96      run_call(websocket, settings, client, ...)
                     + app.state 를 같이 넘기거나, websocket.app.state 를 안에서 읽는다
call_session.py:1452 wants_video, live_model = run_db(...)  ← ⭐ 여기서 backend 도 같이 읽는다
                     (⚠ 한 번의 run_db 로 셋을 같이 — 두 번 부르면 그 사이 구독이 바뀌어
                       「영상은 주는데 모델은 음성」 같은 어긋난 조합이 나온다. 기존 주석 :1449)
call_session.py:2144 client=client                          ← 고른 클라이언트로 교체
```

### 4-5. C1·C2 를 클라이언트 기준으로 바꾼다

```python
core/gemini_live.py
def build_live_config(*, ..., vertex: bool | None = None):
    is_vertex = settings.USE_VERTEX if vertex is None else vertex
    ...
    transparent=True if is_vertex else None,
    **({"safety_settings": _LIVE_SAFETY} if is_vertex else {}),

async def open_session(client, settings, *, ..., vertex: bool | None = None)
```

⚠ **기본값 `None` = 종전 동작**이라 기존 호출부·스냅샷은 바이트 동일이다.

⭐ 더 나은 대안(§7-D3 에서 결정): `vertex` 를 인자로 받는 대신 **클라이언트 객체에서
읽는다** — `_ensure_fresh_credentials` 가 이미 그렇게 한다(C4). `client._api_client._credentials`
유무로 판별하면 호출부가 아무것도 안 넘겨도 자동으로 맞는다. **다만 private 속성 의존이라
SDK 버전에 취약하다.** C4 는 실패해도 무해했지만(경고만), 여기선 틀리면 세션이 안 열린다.

---

## 5. 작업 분해

```
[ ] T1  config: 백엔드별 모델 키 추가 (+ 기존 키 폴백 유지)          — 30분
[ ] T2  call_service: CALL_BACKEND_BY_PLAN + live_backend_for       — 30분
[ ] T3  main: _create_genai_client 에 use_vertex 인자 · 클라 2개 추가 — 1시간
[ ] T4  gemini_live: build_live_config/open_session 에 vertex 축 추가 — 1시간   ⛔ C1·C2
[ ] T5  call_session: 플랜에서 backend 를 같이 읽고 client 를 고른다  — 1시간   ⛔ R4
[ ] T6  C6 방어: 레벨테스트·캐스케이드가 어느 (백엔드,모델) 인지 명시   — 1시간   ⛔ 조용히 터지는 자리
[ ] T7  회귀 갱신 + 신규 (§6)                                        — 2시간
[ ] T8  demo-api 배포 → 실통화 (free/pro/max 각 1건)                 — 30분
```

T1~T5 는 순차. T6 은 T3 이후 병렬 가능.

---

## 6. 검증

### 회귀 (기존 — 깨질 것이 예상되므로 새 계약으로 다시 못박는다)
```
tests/test_gemini_live_config.py   3건이 USE_VERTEX 전역을 흔든다 → vertex 인자 기준으로 전환
tests/test_live_model_name.py      "AI Studio 기준" 주석 → 백엔드별 목록으로
tests/test_plan_call_split.py      표·게이트 → backend 축 추가
```

### 신규 회귀
```
[ ] test_backend_table_matches_the_owner_decision      free/pro=vertex, max=studio
[ ] test_unknown_plan_falls_back_to_free_backend       모르는 플랜 → vertex (R5)
[ ] test_safety_follows_the_client_not_the_global      ⛔ studio 세션엔 safety 0개
[ ] test_transparent_follows_the_client_not_the_global ⛔ studio 세션엔 transparent None
[ ] test_vertex_plan_gets_the_vertex_model_name        (백엔드,모델) 짝이 어긋나지 않는다
[ ] test_studio_plan_gets_the_studio_model_name
[ ] test_level_test_backend_is_explicit                ⛔ C6 — 폴백으로 흘러가지 않는다
[ ] test_cascade_client_is_unchanged                   ⛔ C5 — 기본 클라 의미 불변
[ ] test_default_client_bytes_unchanged                신 설정이 비면 종전과 동일
```

⚠ ⛔ **소스를 읽는 시험**을 하나 둔다 — `test_plan_call_split.py:123` 의 전례대로.
진리표를 스스로 만들어 검증하는 동어반복은 9/4 사고를 못 잡았다.

### 실통화 (demo-api)
```
[ ] free  → Vertex 2.5   5분 완주 · 1011 0건 · 루프 0건 · 응답지연 중앙 < 1.5초
[ ] pro   → Vertex 2.5   같은 기준
[ ] max   → Studio 3.1   표정 전환 정상 · safety 미실림으로 1007 안 남   ⛔ C1
[ ] 레벨테스트           어느 백엔드로 갔는지 로그로 확인               ⛔ C6
[ ] 캐스케이드           대답 LLM 이 안 옮겨갔는지 확인                 ⛔ C5
[ ] 통화후 분석          정상 (JUDGE_MODEL 경로)
```

---

## 7. ⛔ 사장님 결정이 필요한 것

**D1. 통화후 분석·캐스케이드는 어느 백엔드에 두나.**
지금 기본 클라이언트 하나를 셋이 공유한다(C5). 저는 **기본을 안 건드리는 쪽**을 권한다 —
통화만 고르고 나머지는 지금 그대로. 그러면 변경 표면이 통화 하나로 좁아진다.

**D2. 레벨테스트는 어느 백엔드인가.**
플랜 분기를 안 탄다(C6). Free 사용자도 보는 기능이라 **Vertex 2.5** 가 자연스럽지만,
레벨테스트 프롬프트는 code-switching 규칙이 반대라 별도 검증이 필요하다.

**D3. 백엔드 판별을 인자로 넘길 것인가, 클라이언트 객체에서 읽을 것인가**(§4-5).
저는 **인자**를 권한다 — private 속성 의존은 틀리면 세션이 안 열린다.

**D4. `safety_settings` 없이 Max 페르소나가 검열되는가.**
코드가 스스로 **미검증**이라 적어 뒀다(`gemini_live.py:302`): *"AI Studio 에서는 그 완화가
안 걸리므로, 그쪽으로 운영을 옮길 거면 페르소나가 검열되는지 먼저 확인해야 한다."*
⚠ Max 는 **이미 3.1(AI Studio)로 돌고 있다** — 즉 이 위험은 이번 변경이 만드는 게 아니라
**이미 켜져 있다.** 이번에 같이 확인할 것인지만 정하면 된다.

---

## 8. 착수 전 선행 실측 2건

**A. ⭐ Vertex 2.5 의 루프 발생률.**
AI Studio 2.5 는 7%(3/43), 3.1 은 0%(0/277)였다. Vertex 2.5 는 **미측정**이다.
여기서도 7% 가 나오면 "빠른데 하하하 하는" 모델이라 갈 이유가 크게 약해진다.
⇒ 맨몸 세션 30~40턴이면 갈린다. **가장 먼저 한다.**

**B. Vertex 단가가 AI Studio 와 같은가**(C8). 다르면 `LIVE_TOKEN_PRICE_USD` 를 백엔드별로
갈라야 하고, 안 그러면 원가 계기판이 **조용히 틀린 값**을 낸다.

**C.** (부수) Vertex Live 동시 세션 쿼터. AI Studio 는 60(메모리 기록).
Vertex 쿼터를 모르면 사용자가 늘 때 조용히 막힌다.

---

## 9. 계정 · 자격증명 (실측)

```
프로젝트        bt-dev-web-01   (번호 333511894671)   ← 7/24 이후 계속 이것
                ⚠ 7/15~7/24 는 tta-lingko-rookie 였다. .env.local 만 아직 그것을 가리킨다(로컬 전용)
리전            us-central1
키              Secret Manager  beavertalk-app-gcp-key  (v1 6/25, v2 7/24, v3 7/25)
                → 컨테이너 /secrets/gcp_key.json 에 마운트
Cloud Run SA    333511894671-compute@developer.gserviceaccount.com  (roles/editor)
```

⛔ **`/secrets/gcp_key.json` 안의 서비스계정이 누구고 `roles/aiplatform.user` 를 갖는지
확인하지 못했다** — 시크릿 값을 열어야 알 수 있고 그건 R6 대상이다. 배포 전에 확인한다.
프로젝트 IAM 에 `aiplatform.user` 를 가진 SA 가 셋 보인다:
`ais-gemini-key-b6ab7a9e77bd490@…` · `web-stt-pronunciation-challeng@…` · `vertex-express@…`(expressUser).

⚠ 참고로 이번 A/B 는 **SA 키를 열지 않고** 사장님 gcloud ADC(`hahahoho3797@gmail.com`,
roles/owner)로 붙어서 쟀다. **테스트가 통했다고 배포가 통한다는 보장이 없다** — 권한 주체가 다르다.

---

## 10. 되돌리기

```
1. Cloud Run env 에서 신 설정 키 제거  → live_model_for 가 종전 키로 폴백 → 종전 동작
2. CALL_BACKEND_BY_PLAN 을 전부 "studio" 로  → 코드 배포 없이 표만 바꿔도 되게 설계한다
```

⭐ 2번이 되려면 표가 **env 로 덮이는 값**이어야 한다. 설계에 반영한다.

---

## 11. 기록해야 할 정정 (이번 조사에서 드러난 것)

⛔ 저장소에 **틀린 원인 기록**이 남아 있다. 이번 작업에 같이 고친다.

```
config.py:120   "낱말 순서가 뒤집힌 오타였고" → 오타가 아니다. **Vertex 이름**이었다
docs/QUEUE.md Q15  "구글이 모델을 내렸다"    → 내리지 않았다. Vertex 에 그대로 살아 있다
커밋 ca7cec4 메시지  같은 오류
```

**진짜 원인**: `2026-09-06 01:21` demo-api 리비전 `00265-br2` 에서 `USE_VERTEX` 가
true → false 로 뒤집혔는데 **모델 이름을 안 바꿨다.** 그 이름은 Vertex 전용이라
AI Studio 에서 1008 이 났고, 그게 Free·Pro 통화 장애였다.
(app-api 는 8/20 `00072-msj` 에서 같은 전환을 하며 이름도 같이 바꿔 무사했다.)

⇒ 교훈: **`USE_VERTEX` 와 모델 이름은 같이 움직여야 한다.** 이번 설계의 (백엔드, 모델)
쌍 표가 그 교훈을 구조로 만든다 — 한쪽만 바꾸는 것이 불가능해진다.
