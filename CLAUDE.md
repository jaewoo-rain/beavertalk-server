# BeaverTalk Server — 프로젝트 규칙 (CLAUDE.md)

이 문서는 매 세션 로드되는 **백엔드 작업 규칙**이다. 모든 작업은 아래 룰과 사실을 따른다.

## 무엇인가
외국인 학습자를 위한 **한국어 회화 학습 앱**의 백엔드 API.
**FastAPI + SQLAlchemy 2.0 (동기/sync) + Supabase(PostgreSQL)**. 인증은 자체 JWT + Supabase 토큰(통화 WS).
핵심 기능은 **normalcall**: Gemini Live 네이티브 오디오로 비버(선생님 페르소나)와 5분 한국어 음성통화 → 통화후 분석 → 문장 추출 → 복습·발음평가.

## 아키텍처 사실 (추측 금지, 코드가 근거)

### 도메인 수직 슬라이스
```
domains/<도메인>/{ models, schemas, repository, service, routers }
  account/   회원·인증(Supabase find-or-create, soft delete)
  commerce/  캐릭터·음색(voice)·결제·구독·할인
  learning/  통화(call)·발화(sentence)·평가·복습(review)·레벨 + realtime/(WS 통화)
  alarm/     알람·반복요일
```
- **레이어 의존 방향**: `routers → service → repository → models`. routers 는 repository 를 **직접 호출하지 않는다**.
- **routers**: DTO 검증 + 인증(`Depends`) + service 호출 (얇게).
- **service**: 비즈니스 로직 + **트랜잭션 경계**. 쓰기 후 service 가 직접 `db.commit()` (Spring `@Transactional` 자동커밋 아님 — **명시적 커밋**).
- **repository**: 순수 DB 접근(쿼리만, commit 안 함).
- `core/` : 설정·인증·오디오·외부 어댑터(gemini/tts/storage/speechsuper). `db/` : engine·session·base·registry.

### DB / 마이그레이션
- **SQLAlchemy 2.0 동기**(async 아님, psycopg2). 모델은 `Mapped[]` 2.0 스타일.
- 연결 2개: `DATABASE_URL_POOL`(런타임, 6543 pgbouncer) / `DATABASE_URL_DIRECT`(마이그레이션, 5432 직결).
- **스키마 변경은 Alembic 로만.** `Base.metadata.create_all()` 우회 금지. 셋업은 `alembic upgrade head`.
- 모델 변경 → `alembic revision --autogenerate` → 생성 파일 **눈으로 검토**(autogenerate 100% 아님) → `upgrade head` → 모델+마이그레이션 **같은 커밋**.

### normalcall 실시간 (⛔ 불변식)
`domains/learning/realtime/` (`ws_router` → `call_session` → `core/gemini_live`).
- **2펌프 + TaskGroup**: 클라→Gemini, Gemini→클라 동시 펌프. `asyncio.timeout` 절대 백스톱(10분). **barge-in off**(비버 발화중 마이크 미전송).
- Gemini Live 네이티브 오디오. **context window compression(sliding window)** 없으면 ~2분에 세션이 닫힘 — 5분+ 통화의 핵심.
- 시계: 5분(`CALL_DURATION_S`) 경과 → 종료 시드 주입(정상 작별), 10분 절대 백스톱, 1분마다 점진 flush(크래시 내성).
- 오디오: 입력 PCM16/16k, 출력 PCM24k. WS **바이너리=오디오, 텍스트=JSON 제어**(discriminated union, `protocol.py`).
- **graceful degradation**: `genai_client` None 이면 통화만 비활성, 앱은 정상 기동. 외부 연동(발음/이메일/소셜/Storage)도 키 없으면 스텁·폴백.

### 프롬프트 규율
- `core/persona_prompt.build_system_instruction` : **LLM 생성 0, 순수 문자열 조립**. 불변식 템플릿 + 캐릭터(role/personality/rules) + 레벨 프로파일 + 흥미 + 이력.
- code-switching: **한국어 10% + 모국어 90%**. 통화 종료 시점은 서버만 결정("[시스템]" 종료 시드 전엔 비버가 먼저 작별 금지).
- `core/*.py` 어댑터는 도메인/DB 를 모른다. system_instruction·voice 는 realtime 이 조립해 넘긴다.

## 작업 규칙 (RULES)

- **R1. 플랜 우선 + docs 기록.** 실제 구현 전, 관련 전문 에이전트로 **플랜을 먼저 수립**하고 `docs/` 에 저장한 뒤 착수한다. 파일명은 `YYYYMMDD_HHmm_<간략한-내용>.md`(기존 `docs/` 컨벤션 준수, 맨 앞 작업일시).
- **R2. Alembic 규율.** 모델을 바꾸면 반드시 마이그레이션을 생성·검토·적용하고 **모델+마이그레이션을 같은 커밋**으로. `create_all` 우회 금지.
- **R3. 명시적 커밋.** 쓰기는 service 에서 `db.commit()`. repository 에서 커밋하지 않는다.
- **R4. 불변식 보호.** normalcall 의 2펌프·절대 백스톱·barge-in off·종료 규약을 바꾸는 변경은 반드시 근거와 함께, 통화 회귀 테스트(`tests/test_normalcall_ws.py`)를 돌린 뒤에만.
- **R5. graceful degradation 유지.** 외부 키/서비스 부재로 앱 전체가 죽으면 안 된다 — 해당 기능만 비활성/스텁.
- **R6. 비밀·파괴적 작업 확인.** JWT_SECRET·서비스계정 키·`.env`·prod 배포·마이그레이션 다운그레이드 등은 사용자 확인 또는 명시적 위임이 있을 때만.
- **R7. CEO 오케스트레이터 전권.** `/beavertalk-dev`(CEO 스킬)가 분해·소집·통합·검증·기록을 총괄한다.

## 테스트 / 검증
- `pytest` (tests/). `scripts/smoke_*.py` 는 **실행 중 서버에 실제 요청**하는 수동 점검(파이테스트 아님).
- API 문서: 서버 실행 후 `/docs`(Swagger). 헬스체크 `/health`. dev 전용 통화 데모 `/__calldemo`.

## 팀 / 오케스트레이션
CEO 스킬 `.claude/skills/beavertalk-dev/`. 전문 에이전트는 `.claude/agents/`. 새 반복 역할은 일회성 프롬프트가 아니라 에이전트/스킬로 **영속화**한다.
