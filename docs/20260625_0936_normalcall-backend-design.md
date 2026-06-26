# 2026-06-25 09:36 · normalcall 백엔드 구현 설계 (통화 코어 + 분석)

> 실시간 한국어 음성통화(normalcall)의 **백엔드 구현이 끝난 시점의 설계 정본**.
> 관련: [통합 플랜](20260625_0726_normalcall-integration-plan.md) · [모델 플랜](20260625_0746_normalcall-models-plan.md) · [12단계 커리큘럼](20260625_korean-level-12-curriculum.md)
> 상태: 모델·마이그레이션·core 어댑터·서비스·realtime WS 구현 완료. 라이브 스모크(Vertex 통화 + 분석) PASS. 결정적 pytest(voice-api-tester) 별도 실행.

---

## 0. 한눈에

기존 동기 SQLAlchemy DDD 앱(`fastapi/SQLAlchemy`)의 `learning` 도메인 위에 **"실시간 음성 + AI 두뇌"**만 얹었다. 데이터/CRUD(Call·Sentence·Evaluation·Review)는 이미 있었고, 새로 만든 것은 ① Gemini Live 통화 ② 통화후 분석 ③ TTS ④ Storage ⑤ WS 브리지다.

```
프론트 ──WS(/api/v1/calls/stream)── ws_router ── call_session(async 오케스트레이션)
                                                      │  (run_db: run_in_threadpool)
                                                      ▼
                          normalcall_service(동기 DB) ── 모델(Call/CallRawData/Sentence/…)
                                                      │
                          core 어댑터: gemini_live · gemini_analysis · tts · storage · persona_prompt
```

설계 원칙(사용자 요구): **"과하게 쪼개지 말 것 — 읽기 편한 최소 분할"**. gagcall(bridge.py 898줄)을 반면교사로, 검증된 코드를 포팅하되 파일 수를 최소화했다.

---

## 1. 레이어와 의존성 방향

3개 계층, **안쪽으로만 의존**(speechsuper.py 가 세운 기존 규율 유지):

| 계층 | 위치 | 책임 | 의존 |
|---|---|---|---|
| **realtime** (async 섬) | `domains/learning/realtime/` | WS transport·인증·오케스트레이션 | → service, core |
| **service** (동기 DB + 도메인) | `domains/learning/service/normalcall_service.py` | DB I/O, 분석 오케스트레이션(프롬프트·스키마 **소유**) | → core, models |
| **core** (외부 어댑터) | `core/*.py` | Gemini/TTS/Storage/프롬프트조립 — 도메인·DB·Session 모름 | → (외부 SDK) |

핵심 규율 2가지(이것만 지키면 나머지는 가독성 기준 자유):
1. **core 어댑터는 도메인을 모른다** — `core/*` 에 모델/Session/프롬프트 import 0. (분석 "프롬프트·스키마"는 도메인 지식이라 service 가 소유, core 는 "구조화 generateContent" 메커니즘만.)
2. **async WS 와 동기 트랜잭션을 한 함수에 섞지 않는다** — async 는 realtime 까지만 침투.

---

## 2. 파일별 책임

### core 어댑터 (외부 시스템, 전부 graceful 폴백)
| 파일 | 책임 | 폴백 |
|---|---|---|
| `core/audio.py` | PCM 포맷 상수(16k 입력/24k 출력) + `pcm16_to_wav` | — |
| `core/gemini_live.py` | Gemini Live 세션(Vertex native-audio): 오디오 양방향 + 입출력 전사 + 단일 voice + **컨텍스트 윈도우 압축** + safety. `LiveSessionProtocol`(모킹), `open_session` CM, `LiveEvent` 정규화 | — (호출부가 client None 체크) |
| `core/gemini_analysis.py` | `generate_structured(client, model, *, system_instruction, prompt, schema)` — response_schema generateContent 1콜 → 파싱된 Pydantic | 실패 시 None |
| `core/tts.py` | `synthesize_korean(text) -> bytes\|None` (Chirp 3 HD) | 미설치/비활성/실패 → None |
| `core/storage.py` | Supabase Storage `upload/public_url/signed_url` | 미설정/실패 → None |
| `core/persona_prompt.py` | `build_system_instruction(role, personality, rules, level_profile, locale, interests, history)` + `SEED_OPENING` | — |

### service (동기 DB + 비동기 분석)
`domains/learning/service/normalcall_service.py`:
- `run_db(session_factory, fn)` — **async↔sync 브리지**(아래 §3).
- 동기 DB: `load_call_setup`(페르소나+레벨profile+voice+locale 한 번에), `create_call`, `save_segments`, `finalize_call`, `set_status`, `get_status`.
- 비동기 분석: `analyze_call(...)` + 스키마 `CallAnalysis`/`LearnedExpression` + `_analysis_instruction`(프롬프트) + `_build_dialog`/`_save_analysis`(dedup 포함)/`_set_sentence_tts`.

### realtime (유일한 async 섬)
| 파일 | 책임 |
|---|---|
| `realtime/protocol.py` | WS 메시지(Client: start/playback_done/ping, Server: turn_start/transcript/turn_end/call_ended/error/pong) + TypeAdapter |
| `realtime/call_session.py` | **통화 본체**: 2펌프 + 시계워처 + 점진 flush(TaskGroup), barge-in off, 절대 백스톱, 선톡/종료 시드, 세그먼트 누적, 종료후 분석 트리거 |
| `realtime/ws_router.py` | WS `/calls/stream`(쿼리 토큰 JWT 인증) + `GET /calls/{id}/status` |

### 인프라 변경
- `main.py` lifespan: `app.state.genai_client = _create_genai_client(settings)`(Vertex/AI Studio, graceful None).
- `domains/learning/routers/__init__.py`: realtime 라우터 include.
- `core/config.py`: GEMINI/Vertex/Supabase 설정. `requirements.txt`: google-genai·texttospeech·supabase.

> **왜 이 분할인가:** gemini_live(실시간 async WS 세션)와 gemini_analysis(1회성 동기 generate)는 **호출 모델이 근본적으로 달라** 분리. tts/storage 는 서로 다른 외부 서비스라 분리. realtime 3파일은 transport/orchestration/protocol 로 책임이 명확히 3등분 — 그 이하 분할은 과분할. 어댑터에 Protocol/ABC·DI 컨테이너는 도입 안 함(ROI 0).

---

## 3. 핵심 결정 — async WS ↔ sync SQLAlchemy 브리지

이 프로젝트는 **전부 동기**(Session, NullPool, 요청스코프 `get_db`)인데 Gemini Live·WS 는 async다. normalcall 은 "프로젝트 안 유일한 async 섬".

**원칙: 통화 중엔 DB 를 안 만지고 메모리 버퍼에 누적. DB 쓰기는 (a) 시작 시 Call INSERT, (b) 1분마다 점진 flush, (c) 종료 시 나머지 flush, (d) 종료후 분석 — 이 지점에서만, 짧게.**

```python
async def run_db(session_factory, fn):
    """별도 스레드에서 새 세션을 열어 fn(db) 실행 후 닫는다(이벤트 루프 비차단)."""
    def _work():
        db = session_factory()
        try: return fn(db)   # 내부에서 명시적 commit(프로젝트 컨벤션)
        finally: db.close()
    return await run_in_threadpool(_work)
```

- WS 핸들러는 `get_db`(요청스코프)를 **쓰지 않고** `app.state.session_factory` 를 `run_db` 로 짧게 연다 → 장수명 WS 가 pgbouncer(6543) 커넥션을 점유하지 않는다.
- **서비스 계층은 100% 동기 유지** → 기존 컨벤션 무손상.
- ORM 객체를 async 컨텍스트로 끌고 가지 않는다: `load_call_setup` 이 role/personality/voice/profile 을 **평범한 str/list** 로 뽑아 넘긴다.

**저장 전략(사용자 결정):** 1분마다 점진 flush(`_periodic_flush`) + 종료 시 나머지(`_persist_remaining`). `persisted_count` 커서로 중복 없이 증분 저장 → 5→10→15분 통화로 늘어도 크래시 시 ≤1분만 유실.

---

## 4. WS 프로토콜 & 통화 시퀀스

오디오 = **바이너리 프레임**(클라→서버 PCM16k, 서버→클라 PCM24k), 제어 = **텍스트 JSON**.

```
1. 클라: WS connect  ?token=<JWT access>           → ws_router 가 decode_token → member_id (실패 1008 close)
2. 클라: {type:"start", character_id, locale?}     → _read_initial_start
3. 서버: run_db(load_call_setup)                    → role/personality/rules/voice/level_profile/locale/interests
        build_system_instruction(...)              → Gemini Live system_instruction
        run_db(create_call)                         → Call(status=ongoing)
4. 서버: asyncio.timeout(ABSOLUTE) 안에서:
        open_session(voice=캐릭터voice) → TaskGroup{ client→gemini, gemini→client, clock, flush }
        send_text_turn(SEED_OPENING)               → 비버 선톡(인사 + "공부/대화?" 모국어로)
5. 통화: barge-in off(비버 발화중 마이크 미전송). 턴 경계에서 세그먼트 확정(user 16k / beaver 24k).
        1분마다 누적 세그먼트 → CallRawData append(role/turn_index/content/voice_url).
6. 종료: clock 이 CALL_DURATION 경과 → should_close → 다음 turn_end 에 종료 시드 1회 →
        비버 작별 → _CallFinished. (백스톱: ABSOLUTE 초과 시 강제.)
7. finally: 남은 세그먼트 flush + finalize(total_time, status=analyzing)
        → create_task(analyze_call)  → call_ended 송신 → playback_done 대기 → WS close
8. 프론트: GET /calls/{id}/status 폴링 → "done" → GET /calls/{id}/result(기존 CallService 재사용)
```

**불변(gagcall 검증 구조 유지):** TaskGroup 2펌프 · `asyncio.timeout` 절대 백스톱 · barge-in off · `_finish_call` · `realtime_input_config` 금지(간헐 무음 버그).

**튜닝 상수**(`call_session.py`, ⚠️ 현재 테스트값):
`CALL_DURATION_S=60`(운영 300), `ABSOLUTE_CALL_TIMEOUT_S=90`(운영 330), `SEED_TO_HANGUP_S=12`, `PLAYBACK_DONE_WAIT_S=2`, `FLUSH_INTERVAL_S=60`, `DEFAULT_CHARACTER_ID=1`(비비).

---

## 5. 페르소나 프롬프트 조립

`character`(DB) + `level`(DB) + `member`(DB) → 한 system_instruction(LLM 생성 0, 조립만):

```
[모국어] member.language(=locale)
[페르소나] role / personality (+ rules 있으면)         ← character
[불변 규칙] 모드분기·종료규약·code-switching·교정·안전·길이   ← 코드 고정 템플릿
[학습자 수준] level.profile (12단계 발화 프로파일)         ← level
[학습자 흥미·소재] member.interests
(+ [최근 학습 이력] history — 후속 패스)
```

- **code-switching**: "설명·농담은 모국어, 가르칠 한국어 표현만 한국어 + 즉시 뜻풀이. 레벨 낮을수록 모국어 비중↑." (정성 "한국어 위주"는 전부 한국어로 쏠려 금지.)
- **선톡**: 세션 오픈 직후 `SEED_OPENING`(텍스트 턴 1회)으로 비버가 먼저 발화 유발.
- **종료**: 시간은 프롬프트로 못 지키게 함(모델은 시간감각 X). 코드 타이머가 `[시스템]` 종료 시드를 turn_end 경계에 주입 → 비버가 핑계 대고 작별.

---

## 6. 통화후 분석 (gemini-2.5-flash, 단일 콜)

비동기·백그라운드(`analyze_call`). 통화 저장과 독립 → 분석 실패해도 통화는 보존.

```
run_db(_build_dialog)        # CallRawData(turn순) → [USER]/[BEAVER] 전사
→ (전사 없으면 status=done 빈결과)
→ gemini_analysis.generate_structured(JUDGE_MODEL=gemini-2.5-flash, schema=CallAnalysis)  # 추출+번역+요약+모드 1콜
→ (None 이면 status=failed)
→ run_db(_save_analysis): Call.summary/mode 저장 + 표현별 Sentence(+Evaluation placeholder), korean 기준 dedup
→ 표현별 tts.synthesize_korean → storage.upload(voice-samples) → Sentence.voice_url(public url)
→ status=done
```

**스키마 → DB 매핑**:
| CallAnalysis | DB |
|---|---|
| summary | Call.summary |
| detected_mode (study/chat/mixed) | Call.mode |
| Expression.korean | Sentence.korean_sentence (+ TTS 입력) |
| Expression.translation | Sentence.native_sentence |
| Expression.source_type (asked/corrected/drilled) | Sentence.source_type |
| (member locale) | Sentence.locale |
| (TTS 합성) | Sentence.voice_url |
| placeholder | Evaluation(점수 None) — 연습 채점 시 채움 |

**JUDGE_MODEL = gemini-2.5-flash**(사용자 결정 — 비용 우선). 비동기라 품질 우선 시 2.5-pro 로 config 한 줄 교체 가능. **환각 방지**: 짧은 통화/표현0 graceful, dedup, "명백한 것만 보수적 추출" 지시.

---

## 7. Storage 버킷 규약

| 버킷 | 공개성 | 대상 | 경로 |
|---|---|---|---|
| `voice-recordings` | private | 통화 원본(CallRawData) | `calls/{member_id}/{call_id}/{turn:04d}_{role}.wav` |
| `voice-samples` | public | 표현 TTS(Sentence) | `tts/{call_id}/{sentence_id}.mp3` |

- TTS 는 public 버킷 → **public URL 을 Sentence.voice_url 에 저장** → 기존 `/result` 엔드포인트가 변경 없이 재생 URL 반환.
- 통화 원본은 private → object key 저장(필요 시 signed_url).

---

## 8. 인증 / 설정

- **WS 인증**: 쿼리 `?token=<JWT>` → `decode_token`(purpose=="access", sub=member_id). `get_current_member`(Depends 기반)는 WS 에 못 쓰므로 별도 헬퍼. `core/deps.py` 무수정. (dev-UUID 폴백 없음 — 실제 JWT.)
- **status 엔드포인트**: `GET /api/v1/calls/{id}/status` 는 기존 `CurrentMember`/`DbSession` deps 재사용.
- **config**(graceful — 미설정이면 통화/분석/저장 비활성, 앱은 정상): `USE_VERTEX, GCP_PROJECT, GCP_LOCATION, GOOGLE_APPLICATION_CREDENTIALS, GEMINI_LIVE_MODEL, JUDGE_MODEL, GEMINI_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_BUCKET_*`.
- **genai 클라이언트**: lifespan 1회 생성. Vertex 키는 설정 경로 → 프로젝트 루트 `gcp_key.json` 폴백.

---

## 9. DB 변경 이력 (이 기능)

마이그레이션 head: `c2d3e4f5a6b7` (적용 완료).
- `b1c2d3e4f5a6` — voice 테이블, level 테이블, character(role/personality/rules/voice_id, −prompt), member(korean_level/interests/example_sentences, language comment), call(status/mode), sentence(source_type).
- `c2d3e4f5a6b7` — call_raw_data(role/turn_index) — 분석 전사 복원용.
- 시드(`scripts/seed.py`): voice 30종, level 12단계(`assets/level/level_profiles_12.json`), 캐릭터 4종(비비=Fenrir 트래시토커).

---

## 10. 검증 (스모크 — 실 Vertex/실 DB)

| 스모크 | 결과 | 근거 |
|---|---|---|
| Gemini Live 통화(선톡) | ✅ PASS | Vertex 연결 → 오디오 26청크(PCM24k)+자막 15+turn_end. 트래시토커가 레벨1이라 영어로 "공부/대화?" 질문(code-switching 동작) |
| 통화후 분석(structured) | ✅ PASS | gemini-2.5-flash: mode=study, summary 정확, 표현 3개(drilled/asked + 번역) 추출 |
| WS 등록·인증 | ✅ PASS | TestClient: 토큰 없이 연결 → 1008 close. `/calls/{id}/status` openapi 등록 |
| 모델/앱 빌드 | ✅ PASS | configure_mappers + create_app OK |

**결정적 pytest**(voice-api-tester, 인메모리 SQLite + 가짜 Live세션/WS + 외부 모킹): 별도 실행 — 결과는 후속 반영.

**적용된 개선:** 분석 표현 dedup(모델이 같은 표현을 가끔 2번 산출 → `_save_analysis` 에서 korean 정규화 dedup).

---

## 11. 남은 것 / 주의

- **운영 전 상수 복귀**: `CALL_DURATION_S 60→300`, `ABSOLUTE_CALL_TIMEOUT_S 90→330`.
- **실행 env**: 실서버 구동 시 `pip install -r requirements.txt`(google-genai/supabase/texttospeech).
- **Cloud TTS**: `texttospeech.googleapis.com` 활성화 + `_VOICE_NAME` 검증 전까지 Sentence.voice_url=None(분석은 정상).
- **다음(Pass 2)**: 프론트(WS 클라이언트 + 통화/결과/연습/지표 화면). 연습 발음 채점은 기존 Review+SpeechSuper 흐름 재사용(멀티파트 업로드 변형 필요 시 별도).
- **history 주입**: 현재 build_system_instruction(history=None). 최근 7일 이력 주입은 후속.
