# 긴 지시문 × tool 동거 — 2단 재개(two-phase) 조사 결과 (2026-08-22)

전제(실측 확정, 되풀이 안 함): `gemini-2.5-flash-native-audio-preview-09-2025` · AI Studio ·
**긴 지시문과 tool 이 같은 setup 에 있을 때만 1011**. 짧은 지시문+tool = 9/9 생존.

조사 재료: google-genai **v2.10.0** 로컬 소스
(`C:\Users\jaewoo\miniconda3\envs\beavertalk-server\Lib\site-packages\google\genai\`,
`version.py:16`) + 공식 문서(ai.google.dev) + 공개 이슈/포럼.

---

## 1. session resumption 으로 config 를 바꿔 재개 — **공식적으로 된다**

### 1-1. 근거 (공식)
- **레퍼런스 원문**: "You cannot update the configuration while the connection is open.
  However, you can change the configuration parameters, **except the model**, when pausing
  and resuming via the session resumption mechanism."
  — https://ai.google.dev/api/live (WebSockets 레퍼런스, SessionResumption 절)
- 공식 포럼 스레드(2026-01 문서 정정 반영): 재개 시 **system instructions 와 tools 를
  바꿀 수 있고, 불변은 model 뿐**. 활성 연결 중 변경은 불가 → "닫고 핸들로 재개하며
  새 파라미터를 넣어라"가 공식 워크어라운드.
  — https://discuss.ai.google.dev/t/unable-to-update-gemini-live-session-configuration-without-closing-the-existing-session/83359
- ⇒ **재개 시 tools 신규 추가는 허용 범위다**(문서상 "configuration parameters except the
  model"에 tools 포함 — 위 포럼 스레드가 명시). 단 "긴 SI 가 든 히스토리 + tools" 재개가
  이 모델의 1011 버그를 다시 밟는지는 **문서가 보증하지 않는다**(§아래 리스크, 측정 필요).

### 1-2. handle 은 언제 오는가
- 공식: "If included, the server will send `SessionResumptionUpdate` messages." /
  "Periodically, the server will send update messages…" — 주기 명시 없음.
  (https://ai.google.dev/api/live, https://ai.google.dev/gemini-api/docs/live-session)
- SDK proto 주석: **resumable=false(빈 handle)** 는 "모델이 생성 중이거나 function call
  실행 중"일 때 — 그 시점 재개는 데이터 유실. `types.py:19596-19602`
  (LiveServerSessionResumptionUpdate).
- 실무 관측(비공식): 핸들은 접속 즉시가 아니라 **첫 모델 턴이 완결된 뒤** 체크포인트에서
  발급되는 것이 보통. "The handle will often be sent after the first model response is
  fully streamed and processed." — https://www.technetexperts.com/gemini-live-session-resumption-none/ (커뮤니티 자료, 참고용)
- AI Studio + native-audio 에서 handle 재개 자체가 실동한다는 실전 보고:
  https://discuss.ai.google.dev/t/repeated-1011-internal-error-encountered-on-gemini-2-5-flash-native-audio-preview-12-2025-mid-session-with-session-resumption/141126
  ("reconnect with session_resumption_handle (success)"). ⚠ 같은 스레드: 재개 후 모델이
  하던 말을 이어하진 않고 **다음 유저 턴을 기다린다**(우리 용도엔 오히려 무해).
- 핸들 유효기간: "Resumable tokens are valid for **2 hr** after the last session's
  termination." — https://ai.google.dev/gemini-api/docs/live-session

### 1-3. `transparent=True` 필요한가 — **아니오, 그리고 AI Studio 에선 금지**
- transparent 는 `last_consumed_client_message_index` 를 추가로 받는 옵션일 뿐(오디오
  재전송 버퍼 최적화용). 재개 자체엔 불필요. `types.py:19841-19844, 19604-19610`.
- **mldev(AI Studio) 경로에서 SDK 가 ValueError 를 던진다**:
  `_live_converters.py:1821-1825` — "transparent parameter is only supported in Gemini
  Enterprise Agent Platform mode". 우리 `build_live_config` 도 이미 USE_VERTEX 분기
  (`core/gemini_live.py:248`).

### 1-4. AI Studio(mldev)에서 handle 발급 — SDK 소스 확인
- `_SessionResumptionConfig_to_mldev` 는 **handle 을 그대로 통과**시킨다(`_live_converters.py:1817-1819`).
  막는 건 transparent 뿐. ⇒ mldev 에서 handle 발급/재개는 SDK 차원 제약 없음.
- 우리 코드는 이미 배선돼 있다: `build_live_config(resume_handle=...)`
  (`core/gemini_live.py:241-249`), `resume_update` LiveEvent 정규화
  (`core/gemini_live.py:416-422`), 스위치 `LIVE_SESSION_RESUMPTION`(기본 False,
  `core/config.py:129`).

---

## 2. setup 이후 tool 을 선언할 다른 통로 — **없다 (전수 확인)**

클라이언트→서버 메시지는 **정확히 4종**이 전부다:
`setup` / `clientContent` / `realtimeInput` / `toolResponse`
- 공식: https://ai.google.dev/api/live — "Session configuration to be sent in the first
  message" 외 3종. tools 필드는 **BidiGenerateContentSetup 에만** 있다.
- SDK: `LiveClientMessage` 4필드 — `types.py:20502-20519`. `LiveClientSetup.tools` —
  `types.py:20139-20145`. `LiveClientContent`(turns/turn_complete 뿐), `LiveClientRealtimeInput`,
  `LiveClientToolResponse` 어디에도 tool 선언 칸 없음.
- ⇒ **결론: setup 밖 tool 선언 통로는 프로토콜에 존재하지 않는다.** 유일한 우회가 §1 재개다.

---

## 3. 지시문을 setup 에 안 넣고 주입하는 합법 경로

### 3-1. `history_config` — Vertex 전용 아님. **v1alpha 미시도**
- SDK 는 **mldev 경로에서도 그대로 보낸다**(차단 없음): `_live_converters.py:726-727,
  993-998`. Vertex 전용이면 `explicit_vad_signal`(720-724)처럼 ValueError 를 던졌을 것.
- **공식 AI Studio 레퍼런스에 historyConfig 가 실려 있다**: BidiGenerateContentSetup 필드
  목록에 `historyConfig` — "Optional. Configures the exchange of history between the
  client and the server." — https://ai.google.dev/api/live
- 그런데 실측 1007("Cannot find field") ⇒ **서버측 프로토가 그 api_version/모델에 아직
  없다**는 뜻. 우리 테스트는 기본 `v1beta` 였다(live.connect 는
  `_http_options.api_version` 을 그대로 쓴다 — `live.py:956,971`; SDK 의 ephemeral token
  등 신기능이 v1alpha 게이트인 전례 — `live.py:971-976`).
- ⇒ **미시도 레버**: `genai.Client(api_key=..., http_options=types.HttpOptions(api_version="v1alpha"))`
  로 다시 시도. 근거는 정황(문서에 존재 + SDK 무차단 + v1alpha 게이트 전례)이므로 **추측**
  — 10분짜리 실험으로 판정하라. 되면 이 기능은 정확히 우리 용도다: "This **initial
  history will not trigger a model call** and **may end with model content**"
  (`types.py:20028-20035`) — 생성을 트리거하지 않고 히스토리를 넣는, 설계된 통로.

### 3-2. `client_content` 다턴(user/model 쌍) 사전 주입 — **가능하고 미시도**
- `turns` 는 애초에 복수 설계다: "For multi-turn queries, this is a **repeated field that
  contains conversation history** and the latest request." — https://ai.google.dev/api/live
  (BidiGenerateContentClientContent.turns) = `types.py:20274-20281`.
- `turn_complete=False` 면 **생성을 트리거하지 않고 누적만** 한다: "Otherwise, the server
  will await additional messages before starting generation." — `types.py:20283-20287`.
- 우리 실측(1턴 통짜 주입 1/6)은 "긴 텍스트 1턴 + 즉시 생성"이었다. **잘게 쪼개
  turn_complete=False 로 누적 → 마지막에 True**, 또는 **user/model 쌍으로 위장한 히스토리**
  는 다른 코드패스다(미측정).

### 3-3. `prefix_turns` — 프로토엔 있으나 **SDK 미노출·setup 소속이라 무용**
- SlidingWindow 문서가 참조한다: "System instructions and any
  `BidiGenerateContentSetup.prefixTurns` will always remain at the beginning" —
  https://ai.google.dev/api/live 및 `types.py:19868-19875`.
- 그러나 `LiveClientSetup`(`types.py:20117-20193`)과 공개 setup 필드 목록 어디에도 없다
  (v2.10.0 전체 grep — 참조 두 곳뿐). 있어도 **setup 안**이라 이번 문제를 못 푼다.

### 3-4. cached_content — **없음 재확인**
`LiveClientSetup`/`LiveConnectConfig` 에 해당 필드 부재(`types.py:20117-20193, 20662~`).

---

## 4. "긴 지시문 × tool" 경계의 공개 근거 — **없다**

이 조합 경계(SI 길이 × tools 동시 존재 시 setup 직후 1011)를 명시한 공식 문서·changelog·
이슈는 **찾지 못했다**. 가장 가까운 것들(전부 다른 축):
- googleapis/python-genai#1832 (기지) — 같은 모델, **tool 스키마 복잡도 단독**으로 내부
  오류. open·p2·구글 응답 없음. https://github.com/googleapis/python-genai/issues/1832
- google/adk-python#3918 — tool **실행 중** 1011(우리는 setup 직후라 다름).
  https://github.com/google/adk-python/issues/3918
- google-gemini/cookbook#1197 — 3.1-live, SI 14K~33K 자에서 **지연**만 보고(사망 아님).
  https://github.com/google-gemini/cookbook/issues/1197
- https://discuss.ai.google.dev/t/gemini-live-audio-websocket-closes-immediately-after-opening/134708
  — setup 직후 무언사망이지만 원인 미규명(raw WS→SDK 전환으로 해소).
⇒ **우리 라운드로빈 실측이 현재로선 이 경계의 유일한 1차 자료다.** 재현 스크립트를
python-genai 이슈로 올릴 가치가 있다(파트너 지원 채널 없이 고칠 다른 길이 없다).

## 5. 우회로 점검

- **`enable_affective_dialog` 는 감정 출력 채널이 아니다.** "The model **adapts its
  response style** to match the expression and tone of the input" — 입력 정서를 인식해
  **말투를 바꿀 뿐**, 클라이언트로 감정 데이터를 내보내지 않는다.
  — https://ai.google.dev/gemini-api/docs/live-guide (v1beta 로 설정, 3.1 Flash Live 미지원 명시)
- **프로토콜상 tool 외에 "소리 아닌 구조화 출력" 채널은 없다**(§2의 4메시지 전수 —
  서버→클라도 serverContent/toolCall/toolCallCancellation/goAway/sessionResumptionUpdate/
  usageMetadata 가 전부, `types.py:19682-19792`).
- ⇒ tool 없이 가려면 **Live 밖에서** 만들어야 한다: 통화후 분석과 같은 계열의
  **사이드카 분류기**(아래 방안 E).

---

## ⭐ 지금 당장 시험 가능한 방안 (성공 확률 순)

### A. 2단 재개(two-phase resumption) — 본명 후보
1단(프리워밍, 유저 입장 전): **긴 지시문 + tools 없음**(실측 7/8 생존) + 재개 핸들 수집.
2단: 닫고 **짧은 지시문 + SET_FACE_TOOL + handle** 로 재개 — 페르소나는 히스토리로 계승.

```python
# ── 1단: 페르소나 각인 + 핸들 수집 (tools 절대 없음) ──
cfg_a = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    system_instruction=long_persona,                     # 3,695자 그대로
    session_resumption=types.SessionResumptionConfig(),  # handle=None, transparent 금지(mldev)
    # (전사·voice·압축은 현행 build_live_config 와 동일)
)
# 연결 후: send_client_content 로 짧은 시드 1턴 → 모델 응답 완결까지 수신(오디오는 버림)
#   ⚠ 핸들은 보통 "첫 모델 턴 완결 후" 온다(§1-2). resumable=True 인 마지막 new_handle 채택.
#   ⭐ 보험: 페르소나 요지를 user 턴으로도 1회 주입해 두면(짧게 쪼개서) SI 교체와 무관하게
#     히스토리에 남는다 — 재개 시 SI 상속 여부가 미정의라서다(아래 리스크 3).

# ── 2단: 실통화 연결 ──
cfg_b = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    system_instruction=short_instruction,                # 30자 — 9/9 생존 확인된 모양
    tools=[SET_FACE_TOOL],
    session_resumption=types.SessionResumptionConfig(handle=handle_from_a),
    ...,
)
```
- 프로젝트 배선: `open_session(resume_handle=...)` 이미 존재(`core/gemini_live.py:524-548`),
  `LIVE_SESSION_RESUMPTION=True` 필요(`core/config.py:129`).
- **리스크(측정으로만 판정)**: ①재개 직후 "긴 히스토리 + tools" 생성이 1/6 실험과 같은
  서버 버그를 밟을 수 있다 ②sliding window 는 히스토리 턴을 밀어낸다(SI·prefix_turns 만
  면제 — `types.py:19868-19875`) → 페르소나-턴은 압축에 밀릴 수 있다. 기존 재접지 파이프
  (`send_reground`)가 보완 ③재개 setup 에서 SI 를 생략하면 이전 SI 가 상속되는지 **문서
  미정의** — B1(짧은 SI 명시) / B2(SI 생략) 두 변형으로 함께 측정 ④1단 왕복 ~2\u20134초
  프리워밍 비용(통화 시작 전 수행으로 흡수).
- 판정법: 기존 라운드로빈 하네스로 [1단 생존율 × 핸들 수령률 × 2단 생존율 × 표정 발화율
  × 페르소나 준수(전사로)] 5지표.

### B. 지연 주입 — 가장 싼 실험 (변형 하나 안 해봤다)
실패한 1/6 은 **setup 직후 첫 시드로** 긴 텍스트를 넣었다. **짧은 지시문+tool 로 열고,
첫 인사 턴이 완결된 뒤**(turn_end 수신 후) 페르소나를 client_content 로 주입은 미측정.
```python
# setup: short SI + SET_FACE_TOOL (9/9 검증된 모양 그대로)
# 첫 turn_end 후:
await session.send_client_content(
    turns=types.Content(role="user", parts=[types.Part(text="[설정] " + long_persona)]),
    turn_complete=False,   # 생성 트리거 없이 누적만 (types.py:20283-20287)
)
```
- 변형: turn_complete=False(누적만) vs True(즉시 응답). 하네스 수정 5분.

### C. 다턴 분할 주입 — 길이 경사(1,172자부터 사망)를 턴 단위로 우회
```python
for chunk in split(long_persona, max_chars=500):        # 69~500자 = 생존 구간
    await session.send_client_content(
        turns=types.Content(role="user", parts=[types.Part(text=chunk)]),
        turn_complete=False,
    )
await session.send_client_content(turns=짧은_시드, turn_complete=True)
```
- 근거: turns 는 히스토리 반복 필드 + turn_complete=False 는 생성 비트리거(§3-2).
  user/model 쌍(가짜 model 턴 포함)으로 넣는 변형도 같은 하네스로 측정 가능.

### D. history_config + v1alpha — 되면 가장 "정석"인 통로
```python
client = genai.Client(
    api_key=..., http_options=types.HttpOptions(api_version="v1alpha"))
cfg = types.LiveConnectConfig(
    system_instruction=short_instruction, tools=[SET_FACE_TOOL],
    history_config=types.HistoryConfig(initial_history_in_client_content=True), ...)
# setup_complete 후: 페르소나를 client_content 히스토리로 전송(모델 호출 비트리거),
# turn_complete=True 후 realtime 시작 (types.py:20028-20035 규약)
```
- v1beta 에선 1007 실측 — **v1alpha 는 미시도**(§3-1, 추측 단계). ⚠ v1alpha 전환이 다른
  필드(전사·압축)에 부작용 없는지 같은 런에서 확인할 것.

### E. tool 폐기 + 사이드카 분류기 — 확실한 폴백 (Live 는 무툴)
긴 지시문 + tools 없음 = 안정(7/8). 표정은 `out_tr` 전사를 **gemini-2.5-flash-lite
generateContent(enum response_schema)** 로 턴마다 분류해 마커 전송.
- 장점: 1011 축 자체를 제거, 지시문 길이 무제한. 단점: 마커가 턴 단위 후행(+0.3~0.7s).
  단 현행 tool 도 실측상 "오디오 0.00초"에 오는 턴 단위 신호였다(`core/config.py:319-320`)
  — 체감 저하는 제한적. 어댑터 규율상 분류 호출은 소비측(call_session) 배선.

---

## 반증·확정된 것 정리
- ❌ setup 밖 tool 선언 통로: **프로토콜에 없음**(§2, 전수 확인).
- ❌ `transparent=True` @ AI Studio: SDK 가 ValueError(`_live_converters.py:1821-1825`).
- ❌ `enable_affective_dialog` = 감정 **출력** 채널 아님(스타일 적응 전용, §5).
- ❌ Live 용 cached_content: 없음(§3-4). `prefix_turns`: SDK 미노출 + setup 소속(§3-3).
- ❌ history_config "AI Studio 미지원" 단정은 이르다 — **v1beta 미지원**까지만 실측.
  공개 레퍼런스에 존재 + SDK mldev 무차단(§3-1). v1alpha 는 미시도.
- ✅ 재개 시 config(tools 포함, model 제외) 변경: **공식 허용**(§1-1).
- ✅ 핸들: mldev 발급 가능(SDK 소스), 보통 첫 모델 턴 완결 후, 유효 2시간(§1-2).
- ✅ "긴 SI × tool" 경계의 공개 1차 자료: **없음** — 우리 실측이 유일(§4).
