# 2026-06-25 07:26 · normalcall 통합 설계 플랜 (실시간 한국어 음성통화 + 통화후 분석)

> 본 프로젝트(`fastapi/SQLAlchemy`, 동기 SQLAlchemy DDD)에 **normalcall**(5분 실시간 한국어 음성통화 → 통화후 AI 분석 → 결과/연습/지표)을 얹기 위한 설계.
> 4개 전문 에이전트(fastapi-architect, clean-architecture, llm-engineer, schema)의 병렬 검토를 통합한 결과.
> **이 문서는 설계만. 구현·마이그레이션·배포는 사용자 승인 후 별도 진행.**

---

## 0. 핵심 통찰 — "데이터는 이미 있다, AI 두뇌만 얹으면 된다"

이 프로젝트는 normalcall 데이터 모델과 CRUD를 **이미 거의 다 갖추고 있다.** `learning` 도메인이 그 골격이다:

| 기존 모델 | normalcall에서의 의미 | 상태 |
|---|---|---|
| `Call` | 통화 1건 | ✅ 존재 (status 컬럼만 추가) |
| `CallRawData` | 통화 원본음성(턴별 전사+voice_url) | ✅ 존재 (그대로) |
| `Sentence` | 배운 표현(한국어+모국어+TTS voice_url) | ✅ 존재 (source_type 선택) |
| `Evaluation` | 발음 4지표(총점/발음/유창성/리듬) | ✅ 존재 (그대로) |
| `Review` | 발음연습 시도(사용자녹음+글자별 상/중/하) | ✅ 존재 (그대로) |
| `Character`(commerce) | **페르소나 + AI 보이스** (사용자: "캐릭터가 AI 보이스들") | ✅ 존재 (live_voice 추가) |
| `core/speechsuper.py` | 발음 채점(실연동+스텁폴백) | ✅ **완성** |
| `CallService`/`ReviewService` | 통화저장/연습채점 | ✅ **완성** |

**빠진 것 = normalcall이 채울 것(=AI 두뇌·실시간 음성):**
1. 실시간 WS 음성통화 (Gemini Live native-audio 양방향 브리지)
2. 통화후 분석 (gemini-2.5-flash 구조화 출력 → 배운 표현 추출+번역)
3. 표현별 TTS (한국어 정답 음성 생성)
4. Supabase Storage 실제 업로드 (현재 voice_url은 문자열 필드만 존재)

→ 따라서 작업의 본질은 **"기존 동기 CRUD 골격 위에 async 실시간 음성 + LLM 어댑터를 최소 침투로 얹는 것"**이다.

---

## 1. 폴더/파일 배치 (최소 분할 — 사용자 강력 요구)

기존 컨벤션(`core/`=외부 어댑터, `domains/`=비즈니스, 동기 Session+명시적 commit)을 **절대 깨지 않으면서** 최소로 추가한다.

```
core/                                  # 외부 시스템 어댑터 (speechsuper.py와 동급, 도메인 모름)
├── gemini_live.py     # [어댑터·async] Gemini Live 양방향 오디오 세션 래퍼. 원시 바이트 in/out.
├── gemini_analysis.py # [어댑터·sync]  run_structured(prompt, schema, transcript)->dict. 모델명/재시도/파싱/폴백만.
├── tts.py             # [어댑터·sync]  synthesize(text)->bytes. 함수 모듈(추상화 X).
└── storage.py         # [어댑터·sync]  upload(bucket, path, bytes)->object_key. 함수 모듈.

domains/learning/realtime/             # normalcall = 프로젝트 안의 "유일한 async 섬"
├── ws_router.py       # WS 엔드포인트: accept·인증·생명주기·프로토콜 루프만(얇게).
├── call_session.py    # 오케스트레이터: gemini_live ↔ 클라소켓 펌프. 세션 미보유. 이벤트 시점에만 짧게 DB.
└── protocol.py        # 클라↔서버 WS 메시지 Pydantic 스키마(얇음).

domains/learning/service/
└── call_service.py    # 기존 + finalize_from_transcript()/save_analysis() 메서드 추가
                       #   (분석 프롬프트/출력스키마 상수 소유 → core.gemini_analysis 호출 → Sentence/Evaluation 매핑·commit)
```

### 왜 이 분할인가 (근거)

- **core에 어댑터 4개**: 프로젝트는 이미 "외부 API = core의 단일 파일 + 스텁 폴백"을 확립(`speechsuper.py`). Gemini Live/분석/TTS/Storage는 전부 외부 시스템 → 같은 자리, 같은 패턴이 가장 읽기 쉽다.
  - `gemini_live`(실시간 async WS 세션)와 `gemini_analysis`(1회성 동기 generate)를 나누는 건 **호출 모델이 근본적으로 달라서**다 — 억지 분할 아님.
  - `tts`/`storage`는 각자 다른 외부 서비스(다른 키·SDK)라 합치면 오히려 가독성 저하.
- **realtime/ 서브패키지 3개**: WS는 라우터에 두기엔 무겁고 service에 두기엔 async라 컨벤션과 충돌. learning 안 작은 서브패키지로 **격리** → "async는 여기까지"가 폴더로 드러남. ws_router(transport)·call_session(흐름)·protocol(스키마) 3등분, 그 이하는 과분할.
- **분석 로직은 service에**: "무엇을 '배운 문장'으로 칠지"는 **도메인 지식**. 프롬프트/출력스키마는 절대 core에 두지 않는다(그게 도메인 누출선). core엔 "구조화 LLM 호출"이라는 메커니즘만. → `ReviewService ↔ core.speechsuper` 관계의 재현.

### 의존성 방향 (지켜야 할 2개 선)
- `realtime/*` → `service/*` → `core/*` (안쪽으로만)
- `core/*` → 도메인 import 0건 (speechsuper 규율 유지)
- learning → commerce 읽기(Character) = **이미 채택된 컨벤션**(`call.character` FK·relationship 이미 존재)이라 정당.

### 의도적으로 안 하는 것 (과설계 차단)
- 어댑터 Protocol/ABC, DI 컨테이너, 별도 transport 추상화, realtime 전용 repository/schemas, 프롬프트 전략패턴 — **전부 ROI 0, 금지.**
- 분석 매핑은 별 파일(`call_analysis_service.py`)로 빼지 말고 **기존 `CallService`에 메서드 추가**(파일 핑퐁 최소화; 200줄 넘으면 그때 분리).

---

## 2. async WS ↔ sync SQLAlchemy 브리지 (가장 중요한 구조 결정)

**원칙: 통화 중에는 DB를 안 만지고 메모리 버퍼에 누적. DB 쓰기는 3지점에서만 — (a) 시작 시 Call INSERT, (b) 종료 시 일괄 저장, (c) 종료 후 백그라운드 분석 저장. 모든 동기 DB 작업은 `run_in_threadpool`로 감싸 짧게.**

이유: 장수명 WS 1연결이 5분간 DB 세션을 점유하면 Supabase pgbouncer(6543, NullPool)와 충돌. 세션은 "필요한 순간에만, 짧게". 기존 `get_db`(요청 스코프)는 WS 수명과 안 맞으므로 **WS에서는 get_db를 쓰지 않고** `app.state.session_factory`(lifespan이 이미 심어둠)를 스레드 안에서 직접 연다.

```python
# 브리지 헬퍼 (call_session.py 또는 작은 db_bridge 헬퍼)
async def run_db(session_factory, fn):
    """별도 스레드에서 새 세션을 열어 fn(session) 실행 후 닫는다. fn 내부에서 명시적 commit."""
    def _work():
        db = session_factory()
        try: return fn(db)
        finally: db.close()
    return await run_in_threadpool(_work)
```

- 서비스 계층은 **100% 동기 유지** → 기존 컨벤션 무손상. async는 `realtime/`에만 침투.
- ORM 객체를 async 컨텍스트로 끌고 가지 말 것: 통화 시작 시 `Character.prompt`/`live_voice`는 **str로 읽어 넘긴다**(세션 분리 문제 방지).

### 통화 중 저장 전략: **종료 후 일괄(A)** 채택
| | A. 종료 후 일괄(권장) | B. 턴마다 즉시 |
|---|---|---|
| DB 부하 | 통화당 2~3회 | 턴마다 세션 churn |
| pgbouncer 적합 | 좋음 | 나쁨 |
| 크래시 내성 | 약함 | 강함 |
| 복잡도 | 낮음 | 높음 |

5분 단발 통화에서 B의 내성 이점은 과함. **A 채택**, 필요 시 "Call은 시작 시 INSERT, raw_data만 N턴 append" 하이브리드로 승급.

### 백그라운드 분석: `asyncio.create_task` fire-and-forget
- WS 종료 직후 분석(수초)을 WS로 붙잡지 않음. `{type:"analysis_started"}` 보내고 WS close → `create_task(analyze_and_save)`.
- 태스크 핸들을 `app.state.bg_tasks: set`에 보관 후 완료 시 discard(유실 방지). 신뢰성 중요해지면 Celery/RQ로 승급할 자리(현재는 in-process로 충분).

---

## 3. 엔드포인트 토폴로지 (충돌·중복 최소화)

**WS는 직접 DB 기록, 조회는 기존 REST 재사용. 기존 `POST /calls`(일괄저장)는 normalcall이 안 씀.**

| 기존 엔드포인트 | normalcall | 비고 |
|---|---|---|
| `POST /calls` (일괄저장) | **미사용** | 클라가 통째 올리는 흐름. normalcall은 서버가 WS로 주관 → 클라가 올릴 게 없음. 건드리지 않고 그대로 둠. |
| `GET /calls`, `/{id}`, `/{id}/result`, `/{id}/raw` | **재사용** | normalcall이 만든 Call/Sentence/Evaluation도 동일 스키마라 결과조회 공짜. |
| `PATCH /calls/{id}` (rating) | **재사용** | 통화후 만족도. |
| `POST /sentences/{id}/reviews` (녹음채점) | **재사용** | 발음연습 = 기존 ReviewService+speechsuper 그대로. |

### 신규 엔드포인트 (단 2개)
```
WS   /api/v1/calls/stream         # 실시간 통화 (유일한 WS)
GET  /api/v1/calls/{id}/status    # 분석 진행상태 폴링 (얇은 REST)
```
- `realtime/ws_router.py`의 APIRouter를 learning `routers/__init__.py`에서 include → `/api/v1` 자동 적용.
- `status`는 `/result`에 합치지 않음(완성데이터 조회 vs 진행상태 폴링 분리가 프론트에 단순).

### WS 메시지 프로토콜 (protocol.py)
오디오는 **바이너리 WS 프레임**(base64 금지, 지연·오버헤드 최소), 제어는 **텍스트(JSON)** 프레임. 프레임 타입(text/bytes)으로 구분.

서버→클라:
```
{type:"ready", call_id, character:{name,image_url}}
(binary)  AI 음성 출력 청크(PCM 24k)
{type:"transcript", role:"ai"|"user", text, final:bool}
{type:"turn_complete"} / {type:"interrupted"}
{type:"time", remaining_sec}            # 5분 카운트다운(선택)
{type:"call_ended", reason:"timeout"|"user"|"error"}
{type:"analysis_started"}               # WS 곧 닫힘
{type:"error", code, message}
```
클라→서버:
```
(binary)  마이크 PCM 16k 청크
{type:"end"} / {type:"ping"}
```

### WS 인증
`get_current_member`는 `Depends(get_db)`+`oauth2_scheme`라 WS에 그대로 못 씀. **realtime/ws_router.py에 WS용 인증 헬퍼** 별도(쿼리 `?token=` 또는 first-message → `core.security.decode_token` → member_id 추출). **`core/deps.py`는 건드리지 않는다.** (이 프로젝트는 실제 JWT(Member, int)가 있으므로 dev-UUID 폴백 불필요.)

---

## 4. 통화 시퀀스 (전체 흐름)

```
1. 클라 WS connect (?token=JWT, character_id)
2. 서버: JWT검증 → run_db(open_call) → Call(status=ongoing) INSERT → {ready}
3. 서버: core.gemini_live.connect(system_instruction = character.prompt + member프로파일 + locale)
        AI 선톡 트리거 → locale로 "공부할래 vs 대화할래?" 물음
4. 양방향 브리지 루프: 사용자 음성 ↔ AI 음성(패스스루), 자막 누적, raw_data 턴 메모리 버퍼
5. 5분 타이머 만료(or 클라 {end}):
        gemini_live 세션 close → {call_ended}
        run_db(finalize_call): raw_data 저장, total_time, status=analyzing
        {analysis_started} → WS close → create_task(analyze_and_save)
6. 백그라운드: gemini_analysis(전사→배운문장) → 각 문장 TTS+Storage 업로드(voice_url)
        run_db(save_analysis): Sentence + Evaluation(placeholder) 생성, Call.summary, status=done
7. 프론트: GET /calls/{id}/status 폴링 → done → GET /calls/{id}/result
```

브리지 지연 핵심 = **오디오 패스스루**(Gemini 바이트를 가공·버퍼 없이 즉시 send_bytes). 자막은 별도 텍스트 프레임이라 오디오를 막지 않음.
조기 끊김 대비: downstream 예외 시 그때까지 버퍼로 부분 finalize, 음성 완전실패면 자막만 raw_data로(텍스트 폴백). `ongoing`인 채 끊긴 Call은 후속 cleanup에서 `failed` 처리(고아 방지).

---

## 5. 통화후 분석 — gemini-2.5-flash 단일 콜

**통화당 generateContent 1회로 충분.** 모드판별 + 표현추출 + 모국어번역 + 한줄요약을 한 콜(response_schema)에서 처리. 다단계 금지(N+1 콜 폭발·컨텍스트 손실).

- 모드는 콜 안 신호(분기 직후 사용자 답변)로 결정적 → `detected_mode` 한 필드.
- 번역은 짧은 표현·같은 컨텍스트 → 추출과 동시 산출(`korean`+`translation`).
- **TTS 텍스트 별도 필드 안 만듦**: `expressions[].korean`을 그대로 TTS 입력(프롬프트에서 korean을 완결·표준 문장으로 강제).
- 분석은 통화 **종료 후 비동기** → 실시간 지연과 충돌 없음. p50 약 2~5초.

### response_schema 초안 (Pydantic)
```python
class DetectedMode(str, Enum): CONVERSATION="conversation"; STUDY="study"; UNKNOWN="unknown"
class SourceType(str, Enum):    ASKED="asked"; CORRECTED="corrected"; DRILLED="drilled"

class Expression(BaseModel):
    korean: str            # 표준 완결 한국어 문장(=TTS 입력, 군더더기/괄호/로마자 없음)
    translation: str       # 사용자 모국어(locale) 번역
    source_type: SourceType
    learner_attempt: str | None = None   # 사용자 실제 발화 원문(corrected/drilled)
    evidence_quote: str    # 전사 속 실제 인용(환각 방지·검증용, 미저장)

class CallAnalysis(BaseModel):
    detected_mode: DetectedMode
    summary: str                 # 모국어 한 줄 요약
    expressions: list[Expression]  # 실제 등장한 것만, 없으면 []
```

### DB 매핑
| 스키마 | DB | 비고 |
|---|---|---|
| `summary` | `Call.summary` | 기존 컬럼 |
| `detected_mode` | `Call.mode`(선택) | 미저장 가능 |
| `Expression.korean` | `Sentence.korean_sentence` | 표현당 Sentence 1행, TTS 입력 |
| `Expression.translation` | `Sentence.native_sentence` | |
| (member 모국어) | `Sentence.locale` | 스키마 아닌 member.language에서 채움 |
| (TTS 합성 후) | `Sentence.voice_url` | 분석 콜은 NULL, 별도 TTS 단계가 채움 |
| `source_type` | `Sentence.source_type`(선택) | |
| placeholder | `Evaluation(점수 NULL)` | create_call 기존 로직(발화당 1:1 생성) |
| `evidence_quote` | 미저장 | 검증 후 버림 |

**핵심: 분석결과 → `CallCreate.sentences[]` 형태로 변환하는 매퍼만 추가**하면 저장은 검증된 기존 `create_call` 트랜잭션 재사용. 새 저장경로 만들지 말 것.

### 환각·엣지 처리 (speechsuper의 "예외 안 던지고 폴백" 철학)
- `evidence_quote` 필수화 + 후처리로 전사 내 실제 substring 검증, 없으면 그 표현 **드롭**.
- 짧은 통화(전사 < 임계: 합산 200자 or USER 2턴) → LLM 콜 생략하고 `UNKNOWN, summary=짧은통화, expressions=[]`(비용 절약).
- LLM 실패(타임아웃/스키마/파싱) → 전부 try/except로 빈 분석 폴백. **통화 저장(call+raw_data)은 분석 실패와 독립적으로 성공**(분석은 raw_data 저장 후 별도 단계).
- 중복 표현 korean 정규화 후 dedup.

---

## 6. 페르소나 프롬프트 조립 (Gemini Live system_instruction)

3변수 = `character.prompt`(역할/톤/성격) + member 프로파일(레벨/흥미/예시) + locale. 고정 골격(선톡·5분·code-switching)은 코드 상수, 변수만 끼움.

섹션 헤더로 우선순위 명확화:
```
[CHARACTER]       ← character.prompt 원문
[LEARNER PROFILE] ← locale, korean_level, interests, example_sentences
[LANGUAGE POLICY] ← code-switching (고정)
[CALL FLOW]       ← 선톡→모드물음→분기→5분종료 (고정)
[HARD RULES]      ← 환각/안전/길이
```

### 핵심 규칙 문구 (과거 시행착오 해결)
- **선톡**: "세션 연결되면 침묵하지 말고 네가 먼저 인사하며 시작. 직후 사용자 모국어({locale})로 '한국어로 대화할까, 표현을 공부할까?' 물어봐." + 세션 오픈 직후 서버가 짧은 시드(빈 turn/`"(통화 연결됨)"`)로 모델 턴 유발.
- **code-switching 균형** (정성 "한국어 위주"는 전부 한국어로 / "모국어 강조"는 전부 영어로 쏠림 → **정량 비율 + 레벨 연동**으로 해결):
  ```
  - 기본은 한국어로 말하되, 핵심 한국어 표현은 곧바로 {locale}로 짧게 뜻을 덧붙인다(통역하듯).
  - 설명·지시·위로 등 의미전달 중요한 말은 {locale}.
  - 레벨 가이드: beginner=한국어 30%/{locale} 70%, intermediate=50/50, advanced=한국어 80%.
  - 한국어 문장은 반드시 표준·완결 형태(학습 표현으로 저장됨).
  ```
  비율은 정성 형용사 아닌 **퍼센트+레벨 매핑**으로. 운영 중 골든셋으로 튜닝.
- **5분 종료**: 타임아웃은 코드(asyncio 타이머)로 강제. 4:30쯤 서버가 system 텍스트("(마무리해 주세요)") 주입 → 모델이 오늘 배운 표현 한 줄 정리 + 작별, 새 주제 안 엶. (프롬프트만으로 시간 지키게 하지 말 것 — 모델은 시간감각 없음.)

---

## 7. DB 모델/마이그레이션 델타

새 테이블 0개. 컬럼 추가만. (사용자: "캐릭터가 AI 보이스" → voice 마스터 테이블 금지, Character가 보이스 카탈로그.)

| 테이블 | 컬럼 | 타입/속성 | 왜 | 판정 |
|---|---|---|---|---|
| `character` | `live_voice` | `Text` nullable | Gemini Live 프리빌트 보이스명(캐릭터=보이스) | **필수** |
| `member` | `korean_level` | `Text` nullable | 통화 난이도/교정 강도 | **필수** |
| `member` | `interests` | `Text` nullable(CSV) | 대화 주제 시드 | **필수** |
| `member` | `example_sentences` | `Text` nullable | 사용자 표현 스타일 시드 | 선택(권장) |
| `member` | `language` comment | comment-only → "모국어(번역 target locale)" | locale 단일 진실원천 명확화 | **필수(comment)** |
| `call` | `status` | `Text` NOT NULL `server_default 'ongoing'` | 비동기 분석 폴링 신호 | **필수** |
| `call` | `mode` | `Text` nullable | 통화 모드 뱃지/통계 | 선택(보류) |
| `sentence` | `source_type` | `Text` nullable | 표현 출처 분류 | 선택(발음연습 sentence 재사용 확정 시 승격) |

- 타입은 전부 `Text`(+ 앱 상수 화이트리스트), **DB Enum 회피**(Gemini 보이스 30종 증가·status 값 변경 시 마이그레이션 부채 방지). `member_reason.ALLOWED_REASONS` 패턴 따름.
- `character.voice_url`은 유지(프리뷰 샘플), `live_voice`=실시간 합성 파라미터로 역할 분리.
- member 레벨/흥미는 **직접 컬럼(대안 A)** 권장 — 흥미는 통계 대상 아닌 프롬프트 주입용이라 정규화 이득 작음. 별도 테이블/JSONB 비추천. (예시 '단어' 별 컬럼은 YAGNI — 생략.)
- `member.language`는 "사용 언어"로 모호 → **모국어(번역 target locale)**로 해석·comment 명확화. `Sentence.locale`/`native_sentence` 번역 타깃의 단일 출처.

### Gemini 프리빌트 보이스 화이트리스트 (앱 상수, 30종)
`Zephyr, Puck, Charon, Kore, Fenrir, Leda, Orus, Aoede, Callirrhoe, Autonoe, Enceladus, Iapetus, Umbriel, Algieba, Despina, Erinome, Algenib, Rasalgethi, Laomedeia, Achernar, Alnilam, Schedar, Gacrux, Pulcherrima, Achird, Zubenelgenubi, Vindemiatrix, Sadachbia, Sadaltager, Sulafat`
→ `domains/commerce/models/character.py`에 `ALLOWED_LIVE_VOICES: frozenset[str]` 동봉. (출처: ai.google.dev/gemini-api/docs/speech-generation)

### Alembic 리비전 (head=`a7b8c9d0e1f2`)
- **R1 (DDL only)** `<rev1>_normalcall_schema_delta`: 위 컬럼 add_column 전부 nullable/server_default → 무중단. `call.status`만 NOT NULL + server_default `'ongoing'`(기존 행 자동 백필). member.language comment 변경.
- **R2 (DML only)** `<rev2>_seed_character_live_voice`: 기존 character 행에 `live_voice` UPDATE(명시 매핑 or 기본 'Aoede' 후 개별지정). (선택) 종료된 기존 call status 보정.
- DDL/DML 분리 = 롤백·리뷰 안전성. 시드를 R1에 섞지 말 것.

---

## 8. Storage 버킷/경로 규약

수명주기/접근성 기준 **2버킷**:

| 버킷 | 공개성 | 용도 | 컬럼 |
|---|---|---|---|
| `voice-samples` | public | 캐릭터 프리뷰·TTS 정답음성(재사용·캐시) | `character.voice_url`, `sentence.voice_url` |
| `voice-recordings` | private(서명URL) | 통화 원본·연습 녹음(개인정보) | `call_raw_data.voice_url`, `review.voice_url` |

경로(member_id 파티셔닝 → 탈퇴 시 prefix 일괄삭제·RLS 단순):
```
voice-recordings/calls/{member_id}/{call_id}/{call_raw_data_id}.webm    # 통화원본(private)
voice-recordings/reviews/{member_id}/{sentence_id}/{review_id}.webm     # 연습녹음(private)
voice-samples/tts/{call_id}/{sentence_id}.mp3                           # 문장 TTS(public)
voice-samples/characters/{character_id}.mp3                             # 캐릭터 샘플(public)
```
- DB엔 **object key(버킷 상대경로)만 저장**, 풀 URL 금지(서명URL 발급·버킷이동 유연).
- 확장자: 사용자녹음 `.webm`(MediaRecorder), 합성 `.mp3`/`.wav`.

---

## 9. 신규 의존성 + config

`core/config.py`에 추가(스텁 폴백 정책을 speechsuper와 동일하게 — 키 없으면 스텁/None):
```
GEMINI_API_KEY / (Vertex면 GOOGLE_APPLICATION_CREDENTIALS, PROJECT, LOCATION)
GEMINI_LIVE_MODEL      = "gemini-live-2.5-flash-native-audio"   # 통화
GEMINI_ANALYSIS_MODEL  = "gemini-2.5-flash"                     # 분석
GEMINI_TTS_MODEL       = "gemini-2.5-flash-preview-tts"         # 또는 별도 TTS
SUPABASE_URL / SUPABASE_SERVICE_KEY
SUPABASE_BUCKET_SAMPLES = "voice-samples"
SUPABASE_BUCKET_RECORDINGS = "voice-recordings"
```
신규 패키지: `google-genai`, `websockets`(genai가 끌어옴), `supabase`(또는 `storage3`). 전부 키 없으면 graceful 스텁(앱 안 깨짐).

---

## 10. gagcall 대비 단순화 / 생략 / 열린 질문

### 단순화·생략 (의도적)
- **별 voice 테이블 없음**(캐릭터=보이스). **별 normalcall 도메인 없음**(learning 재사용). **dev-UUID 폴백 없음**(실제 JWT). **POST /calls 재구현 없음**(WS가 직접 기록 + 기존 result 재사용). **분석 다단계 없음**(1콜). **어댑터 추상화/DI/전략패턴 없음**.
- gagcall의 bridge.py(898줄·7책임) 반면교사 → realtime을 transport/orchestrator/protocol 3파일로만.

### 열린 질문 (사용자 확인 필요)
1. **`member.language`가 정말 모국어(locale)인가?** — `Sentence.locale`/번역 타깃의 단일 출처라 확정 필요. (현 comment "사용 언어"로 모호)
2. **캐릭터별 `live_voice` 매핑** — 기존 character 행들에 어떤 Gemini 보이스를 줄지(개별 지정 vs 일괄 기본). 캐릭터=보이스라면 캐릭터 카탈로그를 먼저 확정해야.
3. **음성 의도분기**(공부/대화) 신뢰성 — 음성 답변을 Live 모델이 자체 분기 vs 서버가 전사로 판별. 폴백 정책.
4. **선택 컬럼 채택 여부**: `call.mode`, `sentence.source_type` 지금 넣을지(UI 요구에 달림).
5. **code-switching 비율(30/50/80)** 초기값 — 골든셋으로 튜닝 필요(korean-linguist 위임 권장).
6. **TTS 엔진** — Gemini TTS vs Cloud TTS(Chirp). 캐릭터 보이스와 TTS 보이스 일치 여부.

### 리스크
- async WS ↔ sync DB 경계가 이 프로젝트 유일의 async 지점 → `run_db`/세션 수명 규율을 깨면 풀 고갈. 구현 시 가장 주의.
- Gemini Live 선톡 트리거(빈 turn vs 시드 텍스트) 실제 SDK 동작 검증 필요(gemini-expert).
- 통화 중 크래시 시 메모리 버퍼 유실(A 전략) — 필요 시 하이브리드 승급.

---

## 11. 다음 단계 (구현 순서 — 승인 후)

1. **DB 델타 (R1/R2 마이그레이션)** + 모델 컬럼 + 화이트리스트 상수. 열린질문 1·2 먼저 확정.
2. **core 어댑터 4개** (speechsuper 폴백 규율). storage·tts 먼저(독립 테스트 쉬움) → gemini_analysis → gemini_live.
3. **CallService에 분석 매핑 메서드** (response_schema + 매퍼 + create_call 재사용).
4. **realtime/ WS** (protocol → ws_router 얇게 → call_session 오케스트레이터 + run_db).
5. **status 폴링 엔드포인트** + 프론트 연동(결과/연습/지표는 기존 REST 재사용).
6. voice-api-tester로 WS 프로토콜·실패 시나리오·분석 폴백 검증.

---

*이 플랜은 fastapi-architect·clean-architecture·llm-engineer·schema 4개 에이전트 검토를 CEO 통합한 결과다. 구현·배포는 사용자 승인 후.*
