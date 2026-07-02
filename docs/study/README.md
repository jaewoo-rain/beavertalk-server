# BeaverTalk 백엔드 학습 교과서 📖

> BeaverTalk 서버에 새로 합류한 개발자를 위한 **인수인계용 교과서**입니다.
> **읽기만 해도 이 백엔드의 구조와 실시간 AI 파이프라인을 이해**하도록 처음부터 친절하게 썼습니다.

## 이 교과서가 가정하는 독자

- **Java Spring**을 다뤄봤음 (대부분의 개념을 Spring에 빗대어 설명)
- Python은 **조금** 읽을 줄 앎 (`async`·타입힌트는 낯설어도 OK)
- 실시간 통신(WebSocket)·AI 연동은 **처음이어도** 됨 (8장에서 그림으로 설명)

한 줄 소개: **외국인이 AI 캐릭터와 전화로 한국어 회화를 연습하고, 통화 후 배운 표현·발음을 자동 복습받는 앱의 백엔드** (FastAPI + SQLAlchemy 2.0 + Supabase + Gemini Live).

---

## 📚 목차 (이 순서로 읽으세요)

### 0부. 오리엔테이션
- **[0장. 큰 그림 — 한눈에 보기](00-overview.md)** ⭐ 여기부터!
  무엇을 하는 서버인지, 스택, 4개 도메인, 3계층, 공부 순서, 용어집, 문서 드리프트 경고.

### 1부. 뼈대와 인프라
- **[1장. FastAPI 골격 (Spring 대응)](01-fastapi-vs-spring.md)**
  `create_app`/`lifespan` 부트스트랩, 라우터 등록, 에러 핸들러, 도메인 수직 슬라이스.
- **[2장. 설정과 DB 인프라](02-config-and-db.md)**
  `.env`/pydantic-settings, Supabase 이중 연결(6543/5432), NullPool, **명시적 커밋 전략**.
- **[3장. 인증 — Supabase Auth 위임](03-auth-supabase.md)**
  README와 다름! 토큰 검증 위임, 회원 자동 프로비저닝, 소프트 삭제.
- **[4장. Alembic 마이그레이션](04-alembic-migrations.md)**
  스키마 버전관리, autogenerate 워크플로, direct URL, registry.

### 2부. 도메인
- **[5장. account 수직 슬라이스 (정석)](05-account-vertical-slice.md)**
  router→service→repository→model→schema 4계층 표준 예제.
- **[6장. 데이터 모델 전체 ERD](06-data-model-erd.md)**
  14개 테이블과 관계(1:1/1:N/N:M) 지도, cascade/restrict, 인덱스.
- **[7장. commerce 도메인](07-commerce.md)**
  캐릭터·구매·결제·구독, 트랜잭션 원자성, 서버측 가격결정.
- **[8장. 실시간 통화 + AI (플래그십)](08-learning-realtime.md)** ⭐이 프로젝트의 심장
  WebSocket + Gemini Live 4-pump, 통화 후 분석, TTS, 발음채점, 복습.
- **[9장. 외부 연동·스토리지](09-external-and-storage.md)**
  Supabase Storage, Gemini/TTS/SpeechSuper, graceful degradation.
- **[10장. alarm · 테스트 · 배포](10-alarm-and-testing-ops.md)**
  알람/스케줄 orphan removal, pytest·smoke, Docker/Cloud Run.

### 3부. 종합
- **[11장. 전체를 하나로](11-putting-it-together.md)**
  통화 하나가 전 시스템을 관통하는 여정, 어디부터 볼지, 한계·다음 스텝.

---

## ⏱️ 시간이 없다면 (최소 경로)

**0장 → 1장 → 2장 → 3장 → 8장 → 11장** 만 읽어도 뼈대는 잡힙니다.

## 🔗 함께 보기 (저장소 `docs/`)

- `docs/ERD.md` — 데이터 모델 상세
- `docs/*plan*.md` — 기능별 설계 의도·이력 (normalcall, review-flow, call-history 등)
- `docs/DEPLOY_CLOUD_RUN.md` — 배포 런북
- 저장소 루트 `README.md` — 빠른 시작 (단, 인증 서술은 옛 정보 — 3장 참고)

> ⚠️ 이 교과서는 **실제 코드 기준**입니다. 각 장의 `../../` 코드 링크로 직접 대조하세요. `README.md`·`openapi.json`은 일부 옛 정보(드리프트)가 있습니다.
