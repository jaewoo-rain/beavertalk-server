# BeaverTalk Server

외국인 학습자를 위한 한국어 회화 학습 앱의 백엔드 API.
**FastAPI + SQLAlchemy 2.0(sync) + Supabase(PostgreSQL)**, 자체 JWT 인증.

> API 명세는 서버 실행 후 Swagger UI(`/docs`)에서 확인하세요. (회원가입은 이메일 인증 없이 이메일+비밀번호로 즉시 완료됩니다.)

---

## 1. 빠른 시작 (Quick Start)

### 1) 사전 준비
- Python 3.11+
- Supabase 프로젝트 1개 (무료 플랜 OK)

### 2) 가상환경 + 의존성 설치
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3) 환경변수 설정
`.env.example`을 복사해 `.env`를 만들고 값을 채웁니다.
```bash
cp .env.example .env      # Windows: copy .env.example .env
```
필수 값은 **DB 연결 문자열 2개**입니다 (Supabase 대시보드 → Connect 에서 복사):

| 변수 | 용도 | 포트 |
|---|---|---|
| `DATABASE_URL_POOL` | 앱 런타임 (pgbouncer 풀러) | 6543 |
| `DATABASE_URL_DIRECT` | **마이그레이션 전용** (직접 연결) | 5432 |

> 비밀번호에 특수문자가 있으면 URL 인코딩 필요 (`@` → `%40`).
> `DATABASE_URL_DIRECT`를 안 주면 `DATABASE_URL_POOL`로 폴백하지만, **마이그레이션은 가급적 5432 직접 연결**을 쓰세요(풀러 뒤에서 DDL이 불안정할 수 있음).

### 4) DB 스키마 만들기 ⭐ (아래 2장에서 자세히)
```bash
alembic upgrade head
```

### 5) 서버 실행
```bash
uvicorn main:app --reload
```
- API 문서(Swagger): http://localhost:8000/docs
- 헬스체크: http://localhost:8000/health
- (dev 전용) 손테스트 콘솔: http://localhost:8000/__console

---

## 2. Alembic 마이그레이션 — 처음이라면 꼭 읽기

### Alembic이 뭔가요?
**DB 스키마(테이블 구조)의 버전 관리 도구**입니다. Git이 코드 이력을 관리하듯, Alembic은 "테이블을 어떻게 바꿔왔는지"를 파일로 기록합니다.

### 왜 필요한가요? — 모델 코드 ≠ 실제 DB
- `domains/*/models/*.py`(ORM 모델)는 **"테이블이 이래야 한다"는 설계도**일 뿐, 실제 DB가 아닙니다.
- 모델을 고쳐도 **Postgres의 진짜 테이블은 저절로 바뀌지 않습니다.** 누군가 DB에 `ALTER TABLE`을 실행해줘야 합니다.
- 그 실행을 기록·재현하는 파일이 `alembic/versions/*.py` 입니다.

### 새 환경에서 DB 만들기 (가장 중요)
빈 DB에서 아래 한 줄이면 끝납니다.
```bash
alembic upgrade head
```
이 명령은 `alembic/versions/`의 마이그레이션을 **1번부터 현재(head)까지 순서대로 재생**해 스키마를 통째로 만들어줍니다.
즉 **새로 받은 사람도 이 한 줄로 똑같은 DB 구조를 얻습니다.** (그래서 `versions/` 파일은 반드시 Git에 커밋되어 있어야 합니다.)

### 자주 쓰는 명령

| 명령 | 설명 |
|---|---|
| `alembic upgrade head` | 최신 스키마까지 적용 (새 환경 셋업 / 변경 반영) |
| `alembic current` | 현재 DB가 몇 번 버전인지 확인 |
| `alembic history` | 마이그레이션 이력 보기 |
| `alembic downgrade -1` | 한 단계 되돌리기 |
| `alembic revision --autogenerate -m "메시지"` | 모델 변경을 감지해 새 마이그레이션 **생성** |

### 모델을 바꿨을 때 워크플로
```bash
# 1) models/*.py 수정 (예: 컬럼 추가)
# 2) 변경을 감지해 마이그레이션 파일 자동 생성
alembic revision --autogenerate -m "member: add nickname"
# 3) 생성된 alembic/versions/xxxx.py 를 눈으로 검토 (autogenerate가 100%는 아님)
# 4) DB에 적용
alembic upgrade head
# 5) 모델 변경 + 생성된 마이그레이션 파일을 같은 커밋으로 git commit
```

> ⚠️ **`Base.metadata.create_all()`로 우회하지 마세요.** 빈 DB엔 테이블을 만들어주지만, 기존 테이블의 컬럼 변경을 못 하고 Alembic 버전 추적(`alembic_version` 테이블)이 깨져서 이후 마이그레이션이 충돌합니다. 셋업은 항상 `alembic upgrade head`로.

---

## 3. 프로젝트 구조

```
domains/                # 도메인별 수직 슬라이스
  account/   { models, schemas, repository, service, routers }   # 회원·인증
  commerce/  { ... }                                             # 캐릭터·결제·구독
  learning/  { ... }                                             # 통화·발화·평가·복습
  alarm/     { ... }                                             # 알람·반복요일
core/
  config.py       # 설정 (pydantic-settings, .env 로드)
  security.py     # 비밀번호 해시 + JWT 발급/검증
  deps.py         # get_db, get_current_member, PageParams
  schemas.py      # 공통 Page/에러 DTO
  social.py       # 소셜 토큰 검증 (스텁)
  email.py        # 이메일 발송 (스텁)
  speechsuper.py  # 발음 채점 (스텁)
db/
  engine.py  session.py  base.py  registry.py
alembic/          # 마이그레이션 (versions/ 가 스키마 이력)
main.py           # 앱 진입점 + 라우터 등록 + 표준 에러 핸들러 + /health
scripts/          # smoke_*.py(수동 테스트), seed.py, inspect_db.py
tests/            # pytest
front/            # 별도 프론트엔드 (Vite) — 독립 디렉토리
```

### 레이어 의존 방향
```
routers → service → repository → models
```
- **routers**: 요청 검증(DTO) + 인증(Depends) + service 호출 (얇게)
- **service**: 비즈니스 로직 + 트랜잭션 경계(`db.commit()`)
- **repository**: 순수 DB 접근 (쿼리만, commit 안 함)
- **models**: ORM 엔티티
- routers가 repository를 직접 호출하지 않습니다.

> 커밋 전략: **명시적 커밋**. `get_db`는 세션 생성/정리만 하고, 쓰기 후 service에서 직접 `db.commit()`을 호출합니다 (Spring `@Transactional` 자동 커밋과 다름).

---

## 4. 테스트

```bash
pytest                 # 전체
pytest tests/test_email.py -v
```

`scripts/smoke_*.py`는 실행 중인 서버에 실제 요청을 보내는 **수동 점검 스크립트**입니다 (pytest 아님).

---

## 5. 환경변수 전체

| 변수 | 필수 | 설명 |
|---|---|---|
| `DATABASE_URL_POOL` | ✅ | 런타임 DB (6543 풀러) |
| `DATABASE_URL_DIRECT` | 권장 | 마이그레이션용 (5432 직접). 미설정 시 POOL 폴백 |
| `ENV` | | `dev`/`prod` (기본 `dev`). dev에서 SQL 로깅·테스트 콘솔 활성 |
| `JWT_SECRET` | prod 필수 | JWT 서명 키. **prod에선 기본값이면 기동 차단**. `openssl rand -hex 32` |
| `SPEECH_SUPER_APP_KEY` / `_SECRET_KEY` | | 발음평가. 없으면 스텁으로 폴백(앱 정상 동작) |
| `SPEECH_SUPER_CORETYPE` | | 평가 coreType (기본 문장평가) |
| `RESEND_API_KEY` / `MAIL_FROM` | | 이메일 실발송. 없으면 콘솔 출력 폴백 |
| `GOOGLE_CLIENT_ID` | | 소셜 로그인(구글) audience. 콤마로 여러 개 |

> 외부 연동(소셜/이메일/발음채점)은 현재 **스텁**이라 키가 없어도 앱은 동작합니다. 운영 전 실연동으로 교체하세요.

---

## 6. 자주 묻는 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| `column "..." does not exist` | 모델은 바꿨는데 마이그레이션 미적용 → `alembic upgrade head` |
| `Target database is not up to date` | 적용 안 된 마이그레이션 있음 → `alembic upgrade head` |
| 마이그레이션이 멈춤/타임아웃 | 풀러(6543)로 DDL 시도 중일 수 있음 → `DATABASE_URL_DIRECT`(5432) 설정 |
| `password authentication failed` | `.env`의 비밀번호 특수문자 URL 인코딩 확인 (`@`→`%40`) |
| prod 기동 시 JWT 에러 | `ENV=prod`인데 `JWT_SECRET`이 기본값 → 강한 무작위 값으로 교체 |
```
