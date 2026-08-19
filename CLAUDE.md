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
  learning/  통화(call)·발화(sentence)·평가·복습(review)·레벨·체크판(learning_item·mastery) + realtime/(WS 통화)
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
- **2펌프 + TaskGroup**: 클라→Gemini, Gemini→클라 동시 펌프. `asyncio.timeout` 절대 백스톱(540s/9분 — 연결 ~10분 선점). **barge-in off**(비버 발화중 마이크 미전송).
- Gemini Live 네이티브 오디오. 세션 한계(압축 無): **오디오 15분 / 연결 자체 ~10분**(S2). **context window compression(sliding window)** 은 세션을 무제한으로 늘리고 오래된 오디오 토큰을 밀어내 **드리프트 완화·장기 통화 대비** — 5분 통화도 이 압축 위에서 돈다.
- 시계: 5분(`CALL_DURATION_S`) 경과 → 종료 시드 주입(정상 작별), 540s 절대 백스톱, 무음 3단 넛지(in_tr 부재로 감지 → 재개→확인→종료 합류), GoAway 예고 시 조기 종료, 1분마다 점진 flush(크래시 내성).
- 오디오: 입력 PCM16/16k, 출력 PCM24k. WS **바이너리=오디오, 텍스트=JSON 제어**(discriminated union, `protocol.py`).
- **graceful degradation**: `genai_client` None 이면 통화만 비활성, 앱은 정상 기동. 외부 연동(발음/이메일/소셜/Storage)도 키 없으면 스텁·폴백.

### 프롬프트 규율
- ⭐ **프롬프트를 만지기 전 `docs/prompts/README.md` 를 먼저 읽는다**(정본 — 카탈로그·엔진 차이·튜닝 원칙·되돌리면 안 되는 지뢰밭·결정 로그). 같은 폴더에 노션 원문 5종이 보존돼 있다(노션은 로그인 필요라 도구로 못 읽는다). 프롬프트를 고치면 그 §8 결정 로그에 적는다.
- `core/persona_prompt.build_system_instruction` : **LLM 생성 0, 순수 문자열 조립**. 불변식 템플릿 + 캐릭터(role/personality/rules) + 레벨 프로파일 + 흥미 + 이력.
- code-switching: 일반 통화는 **한국어 10% + 모국어 90%**. ⚠ **레벨테스트 콜은 이 규칙을 뒤집는다**(안내·리액션=모국어, 측정 질문=한국어). 통화 종료 시점은 서버만 결정("[시스템]" 종료 시드 전엔 비버가 먼저 작별 금지).
- `core/*.py` 어댑터는 도메인/DB 를 모른다. system_instruction·voice 는 realtime 이 조립해 넘긴다.
- 통화 유형 2종(같은 엔진 + 대본만 교체): `build_system_instruction`(일반) / `build_leveltest_instruction`(레벨테스트). 공부/대화 블록·승급 알림·힌트는 신 인자 None 이면 출력 바이트 동일(하위호환).

### 레벨 시스템 (2026-07 신규 — 상세는 docs 3부작)
외국인 학습자에게 **레벨테스트 → 체크판 → 자동 레벨업**을 서버가 자동으로 돌린다. 레벨은 마이페이지에 **노출**한다(2026-07-31 D2 폐기 — 종합 레벨 카드 + 레벨별 고정 "상위 N%" `service/level_percentile.py`). 체크판 항목별 상태·진행률은 여전히 비노출.
- **레벨 13단계**: L1 생존회화(청크 46) + L2~13 = CEFR A1~C4. `member.korean_level`(1~13). 커리큘럼 마스터 `learning_item`(문법 459 + 어휘 10,636 + 청크 46 ≈ 1.1만 행).
- **체크판 3테이블**: `member_item_progress`(희소 — 행 부재=미학습), `item_evidence`(append-only 감사 로그 — **상태·승급의 원본, UPDATE 금지**), `member_level_history`(승급 이력·멱등 키).
- **관통 원칙 3**: ①AI는 증인 코드가 심판(LLM 판정은 인용 검증 통과해야 데이터) ②증거가 원본·나머지는 파생 계산(별도 플래그 금지) ③선별은 SQL·AI엔 골라서 떠먹임(벡터DB 불필요).
- **서비스**: `domains/learning/service/mastery_service.py`(증거·상태전이·fast-track·evaluate_level_up·grandfathering) + `repository/mastery_repository.py`(선별·게이트 집계 순수 SELECT). 통화 시작 선별·주입 + 통화후 검출·레벨업은 `normalcall_service.py`.
- **마이그레이션 Rev1~5**: 13레벨 shift(파괴적·prod 백업 필요) → learning_item → call 판정 컬럼 → 체크판 3테이블 → D15 컬럼 정리. dev DB 는 적용 완료.
- **결정 D1~D16**: docs 마스터 플랜 §0. 핵심 — 승급=문법 전용(D12), 잘씀=성공 3회(D14), 체류 게이트 폐지(D15), 동적 힌트(D16).
- **문서 3부작**: `docs/20260709_1231_*-master-plan.md`(결정·플로우) / `_1346_*-detailed-mechanics.md`(동작 명세 ①~⑬) / `_1621_*-overview-for-stakeholders.md`(대외 소개). 구현 요약은 `docs/plans/2026-07-09-level-system-build.md`.

## 작업 규칙 (RULES)

- **R1. 플랜 우선 + docs 기록.** 실제 구현 전, 관련 전문 에이전트로 **플랜을 먼저 수립**하고 `docs/` 에 저장한 뒤 착수한다. 파일명은 `YYYYMMDD_HHmm_<간략한-내용>.md`(기존 `docs/` 컨벤션 준수, 맨 앞 작업일시).
- **R2. Alembic 규율.** 모델을 바꾸면 반드시 마이그레이션을 생성·검토·적용하고 **모델+마이그레이션을 같은 커밋**으로. `create_all` 우회 금지.
- **R3. 명시적 커밋.** 쓰기는 service 에서 `db.commit()`. repository 에서 커밋하지 않는다.
- **R4. 불변식 보호.** normalcall 의 2펌프·절대 백스톱·barge-in off·종료 규약을 바꾸는 변경은 반드시 근거와 함께, 통화 회귀 테스트(`tests/test_normalcall_ws.py`)를 돌린 뒤에만.
- **R5. graceful degradation 유지.** 외부 키/서비스 부재로 앱 전체가 죽으면 안 된다 — 해당 기능만 비활성/스텁.
- **R6. 비밀·파괴적 작업 확인.** JWT_SECRET·서비스계정 키·`.env`·prod 배포·마이그레이션 다운그레이드 등은 사용자 확인 또는 명시적 위임이 있을 때만.
- **R7. CEO 오케스트레이터 전권.** `/beavertalk-dev`(CEO 스킬)가 분해·소집·통합·검증·기록을 총괄한다.

## 테스트 / 검증
- **파이썬은 반드시 conda env**: `PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python -m pytest tests/ -q` (base 파이썬엔 의존성 없음). 한글 출력이 콘솔에서 깨지면 스크립트가 **UTF-8 파일로 쓰게 하고 Read** 로 확인.
- `pytest` (tests/). `scripts/smoke_*.py` 는 **실행 중 서버에 실제 요청**하는 수동 점검(파이테스트 아님).
- API 문서: 서버 실행 후 `/docs`(Swagger). 헬스체크 `/health`. dev 전용 데모: `/__levelcalldemo`(레벨테스트·힌트 체험) · `/__cascadedemo`(캐스케이드 통화).
  ⚠ `/__calldemo` 는 **삭제했다**(2026-08-12) — `scripts/call_demo.html` 이 `aaa14b6` 에서
  지워진 뒤로 라우트만 남아 **계속 500** 이었다. 참조는 문서뿐이었고 코드·프론트는 0건.

## 운영 (dev — 상세는 docs/plans/2026-07-09-level-system-build.md 배포 노트)
- **Cloud Run 서비스**: `beavertalk-app-test-api`(구코드) / `beavertalk-app-demo-api`(레벨 시스템 신코드). 프로젝트 `bt-dev-web-01`, 리전 `asia-northeast3`, **둘 다 같은 dev Supabase DB**(마이그레이션·시드 적용됨).
- **배포**: `scripts/deploy_demo.sh [태그]` — `builds submit --tag`(멀티매니페스트 회피) → 그 이미지로 deploy → 헬스체크. `--source` 직접 배포는 "Container import failed" 로 실패.
- **`.gcloudignore` 는 `.dockerignore` 와 별개 유지**(gitignore 변경이 빌드 업로드를 오염시키지 않게). `.gitignore` 에 `scripts/` 넣지 말 것.
- **dev 도구**: `scripts/dev_levelup_seed.py <이메일>`(승급 직전 시딩), `scripts/dev_inspect_call.py <call_id>`(전사·문장·증거·레벨 덤프), `POST /__dev/level-reset`(레벨 백지화).

## 팀 / 오케스트레이션
CEO 스킬 `.claude/skills/beavertalk-dev/`. 전문 에이전트는 `.claude/agents/`. 새 반복 역할은 일회성 프롬프트가 아니라 에이전트/스킬로 **영속화**한다.
