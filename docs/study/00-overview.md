# 0장. 큰 그림 — BeaverTalk 백엔드 한눈에 보기

> **이 교과서는 누구를 위한 것인가요?**
> BeaverTalk 서버에 새로 합류한 개발자를 위한 인수인계용 교과서입니다.
> 다음을 가정합니다:
> - **Java Spring**을 다뤄봤어요 (그래서 대부분의 개념을 Spring에 빗대어 설명합니다).
> - Python은 조금 읽을 줄 알아요 (`async`/타입힌트는 낯설어도 됩니다).
> - 실시간 통신(WebSocket)·AI 연동은 처음이어도 괜찮아요 — 8장에서 그림으로 풀어냅니다.

---

> 📘 **이 장을 읽고 나면**
>
> - BeaverTalk이 **무엇을 하는 서버인지** 한 문장으로 말할 수 있습니다.
> - 기술 스택과 **4개 도메인**의 지도를 갖게 됩니다.
> - 요청 하나가 거치는 **3계층(router→service→repository)**의 큰 흐름을 이해합니다.
> - **어떤 순서로 챕터를 읽어야** 자연스러운지 로드맵을 얻습니다.
> - 문서와 실제 코드가 **어긋난 부분(드리프트)**을 미리 알고 시작합니다.

---

## 1. BeaverTalk을 한 문장으로

> **"외국인이 AI 캐릭터와 전화로 한국어 회화를 연습하고, 통화가 끝나면 배운 표현·발음을 자동으로 복습받는 앱의 백엔드."**

조금 풀어볼게요. 사용자는:

1. 마음에 드는 **캐릭터(페르소나)**를 고르고 (무료/유료, 결제·구독)
2. 그 캐릭터에게 **실시간 음성 전화**를 겁니다 → AI가 한국어 선생님처럼 대화하거나 가르쳐 줍니다
3. 통화가 끝나면 서버가 **대화를 분석**해 "오늘 배운 문장"을 뽑아내고, 각 문장의 **음성(TTS)**을 만들어 줍니다
4. 사용자는 그 문장을 다시 녹음해 **발음 점수**를 받으며 복습합니다
5. **알람**을 걸어두면 정해진 요일/시간에 학습을 리마인드합니다

즉 이 백엔드는 **① 평범한 CRUD 웹 API**(회원·캐릭터·결제·알람)와 **② 실시간 AI 음성 파이프라인**(통화·분석·발음채점)이 한 몸에 있는 시스템입니다. ②가 이 프로젝트의 진짜 매력이자 난이도예요(8장).

---

## 2. 기술 스택 — Spring 개발자를 위한 대응

| 역할 | BeaverTalk (Python) | Spring 세계의 대응 |
|------|---------------------|--------------------|
| 웹 프레임워크 | **FastAPI** | Spring Boot (MVC) |
| ORM | **SQLAlchemy 2.0** (sync) | JPA / Hibernate |
| DB 마이그레이션 | **Alembic** | Flyway / Liquibase |
| 데이터베이스 | **Supabase (PostgreSQL)** | RDS PostgreSQL |
| 인증 | **Supabase Auth (위임)** | OAuth2 Resource Server + 외부 IdP |
| 설정 | **pydantic-settings** (`.env`) | `application.yml` + `@ConfigurationProperties` |
| 요청/응답 검증 | **Pydantic 모델** | DTO + `@Valid` |
| 의존성 주입 | **`Depends(...)`** | `@Autowired` |
| 실시간 통신 | **WebSocket** + **Gemini Live** | STOMP/WebSocket + 외부 AI SDK |
| 컨테이너/배포 | **Docker + Cloud Run** | Docker + ECS/K8s |

> 핵심 차이 하나만 미리: **트랜잭션을 자동으로 커밋해주는 `@Transactional`이 없습니다.** 이 프로젝트는 service 계층에서 **직접 `db.commit()`**을 호출하는 "명시적 커밋" 전략을 씁니다 (2장에서 자세히). 잊으면 저장이 안 돼요.

---

## 3. 전체 부품 지도 — 4개 도메인 + 코어

이 서버는 **도메인 수직 슬라이스**로 나뉩니다. 각 도메인 폴더 안에 그 기능의 모든 계층(모델·스키마·리포지토리·서비스·라우터)이 모여 있어요.

```
main.py                      앱 진입점: create_app() 팩토리 + lifespan + 라우터 등록 + /health
  │
  ├── domains/account/       👤 회원 · 인증(Supabase 위임) · 온보딩
  ├── domains/commerce/      💳 캐릭터 · 구매 · 결제 · 구독 · 할인
  ├── domains/learning/      🎙️ 실시간 통화 · 문장 · 평가 · 복습  ← 플래그십
  │     └── realtime/        WebSocket + Gemini Live 통화 엔진
  └── domains/alarm/         ⏰ 알람 · 반복요일 스케줄

core/                        공통 인프라
  ├── config.py  deps.py     설정 · 의존성(get_db, get_current_member)
  ├── supabase_auth.py       Supabase 토큰 검증
  ├── gemini_live.py  tts.py  persona_prompt.py  gemini_analysis.py   AI 파이프라인
  ├── speechsuper.py  storage.py  audio.py                            발음채점 · 파일저장 · 오디오
db/                          engine.py · session.py · base.py · registry.py (SQLAlchemy)
alembic/                     DB 스키마 버전 이력(versions/)
```

각 도메인은 **똑같은 4계층 패턴**을 따릅니다:

```
routers/    (HTTP 껍데기 + 인증)      ← Spring @RestController
   ↓
service/    (비즈니스 로직 + db.commit())  ← Spring @Service (트랜잭션 경계)
   ↓
repository/ (쿼리만, 커밋 안 함)        ← Spring @Repository
   ↓
models/     (ORM 엔티티)              ← JPA @Entity
schemas/    (Pydantic DTO)           ← 요청/응답 DTO
```

> **규칙**: 위 계층은 아래 계층만 부릅니다. `routers`는 `repository`를 **직접 부르지 않고** 반드시 `service`를 거칩니다. 이 규칙이 5장(account 도메인)에서 실제 코드로 증명됩니다.

---

## 4. 요청 하나의 큰 흐름

평범한 REST 요청(예: 캐릭터 구매)이 어떻게 흐르는지 미리 큰 그림만:

```
클라이언트 ──(Authorization: Bearer <Supabase JWT>)──▶ POST /api/v1/characters/{id}/purchase
   │
   ▼  get_current_member: Supabase에 토큰 검증 위임 → 회원 자동 조회/생성       (3장)
router (얇게)  ──▶  service.purchase()
   │                   ├─ 비즈니스 규칙(중복구매 방지, 서버측 가격계산)
   │                   ├─ repository로 DB 조회/기록
   │                   └─ db.commit()  ← 여기서 트랜잭션 확정                  (2장·7장)
   ▼
Supabase PostgreSQL
```

실시간 통화는 이것과 완전히 다른, WebSocket 기반의 흐름을 탑니다 — 그건 8장의 주제입니다.

---

## 5. 어떤 순서로 공부할까요? (로드맵)

앞 장이 뒤 장의 재료가 되도록 배치했습니다.

### 📘 1부: 뼈대와 인프라
| 장 | 제목 | 왜 먼저 |
|----|------|---------|
| **1장** | [FastAPI 골격 (Spring 대응)](01-fastapi-vs-spring.md) | 앱이 어떻게 뜨고 요청이 어떻게 라우팅되는지 |
| **2장** | [설정과 DB 인프라](02-config-and-db.md) | Supabase 연결·세션·**명시적 커밋** — 모든 도메인의 기반 |
| **3장** | [인증: Supabase Auth 위임](03-auth-supabase.md) | 모든 보호 엔드포인트의 전제 (README와 다름!) |
| **4장** | [Alembic 마이그레이션](04-alembic-migrations.md) | 스키마를 어떻게 만들고 바꾸나 |

### 🎯 2부: 도메인
| 장 | 제목 | 무엇을 배우나 |
|----|------|---------------|
| **5장** | [account 수직 슬라이스 (정석)](05-account-vertical-slice.md) | 4계층 패턴의 표준 예제 |
| **6장** | [데이터 모델 전체 ERD](06-data-model-erd.md) | 14개 테이블과 관계 지도 |
| **7장** | [commerce 도메인](07-commerce.md) | 구매·결제·구독, 트랜잭션 원자성 |
| **8장** | [실시간 통화 + AI (플래그십)](08-learning-realtime.md) | **이 프로젝트의 심장** |
| **9장** | [외부 연동·스토리지](09-external-and-storage.md) | Gemini·TTS·발음채점·graceful degradation |
| **10장** | [alarm · 테스트 · 배포](10-alarm-and-testing-ops.md) | 알람, 테스트 전략, Cloud Run |

### 🔧 3부: 종합
| 장 | 제목 | 무엇을 배우나 |
|----|------|---------------|
| **11장** | [전체를 하나로](11-putting-it-together.md) | 통화 하나가 전 시스템을 관통, 어디부터 볼지 |

> **최소 경로**: 시간이 없다면 **0 → 1 → 2 → 3 → 8 → 11**만 읽어도 뼈대는 잡힙니다.

---

## 6. 미리 익히는 용어집

| 용어 | 쉬운 뜻 |
|------|---------|
| **도메인 수직 슬라이스** | 기능(account 등)별로 모델·서비스·라우터를 한 폴더에 몰아둔 구조 |
| **명시적 커밋** | 프레임워크가 아니라 개발자가 `db.commit()`을 직접 호출 |
| **Supabase** | Postgres + Auth + Storage를 묶어 파는 BaaS. 이 앱의 DB·인증·파일저장 |
| **pgbouncer / 6543·5432** | Supabase의 커넥션 풀러. 런타임은 6543, 마이그레이션은 5432 직결 |
| **Supabase Auth 위임** | 우리가 JWT를 만들지 않고, 토큰 검증을 Supabase에 맡김 |
| **프로비저닝(provisioning)** | 첫 로그인 시 회원 레코드를 자동 생성하는 것 |
| **Gemini Live** | 구글의 실시간 음성 대화 AI. 통화의 핵심 |
| **TTS** | Text-To-Speech. 배운 문장을 음성으로 합성 |
| **SpeechSuper** | 발음 채점 외부 API (없으면 stub 점수) |
| **graceful degradation** | 외부 서비스가 없어도 앱은 죽지 않고 기능만 축소 |
| **Alembic** | DB 스키마 버전 관리 도구 (Flyway 사촌) |
| **ORM / SQLAlchemy** | 테이블을 파이썬 객체로 다루는 도구 (JPA 사촌) |

---

## 7. ⚠️ 시작 전 경고 — 문서 드리프트

이 저장소는 빠르게 발전 중이라 **일부 문서가 코드보다 뒤처져 있습니다.** 헷갈리지 않도록 미리 짚어둡니다:

- **`README.md`는 "자체 JWT 인증"이라고 하지만 틀립니다.** 실제로는 **Supabase Auth에 위임**합니다. `core/security.py`는 삭제됐어요 (3장에서 정정).
- **`openapi.json`에는 `/api/v1/auth/signup`·`/login` 같은 옛 엔드포인트가 남아 있지만**, 현재 코드의 account 라우터엔 그런 게 없습니다 (Supabase로 이관됨).
- `requirements.txt`의 `bcrypt`·`pyjwt`는 **잔재**(현재 미사용). `scripts/smoke_common.py`는 삭제된 `core.security`를 import해 깨져 있을 수 있습니다.

> 이 교과서는 **실제 코드를 기준**으로 씁니다. 각 장의 코드 링크(`../../` 경로)를 클릭해 직접 대조하세요. 코드가 최종 진실입니다.

---

## ✍️ 스스로 점검

1. BeaverTalk을 한 문장으로 설명해 보세요. (힌트: 외국인, 실시간 통화, 복습)
2. 이 서버의 4개 도메인 이름과, 각 도메인이 공통으로 갖는 4계층을 말해 보세요.
3. 이 프로젝트의 인증은 왜 "자체 JWT"가 아니라 "위임"인가요? README를 그대로 믿으면 안 되는 이유는?

---

⟵ (처음) ・ [📚 목차](README.md) ・ [다음: 1장. FastAPI 골격 →](01-fastapi-vs-spring.md) ⟶
