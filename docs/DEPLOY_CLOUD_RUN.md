# BeaverTalk App API — Cloud Run 배포 가이드

이 FastAPI 서버(앱 백엔드)를 기존 GCP 프로젝트 **`bt-dev-web-01`** 에 Cloud Run 서비스
**`beavertalk-app-api`** 로 올리는 단계별 따라하기 문서. (기존 웹 `beavertalk-api`가 있는 그 프로젝트)

- DB는 외부 **Supabase**라 Cloud SQL 불필요. Cloud Run이 Supabase 풀러(6543)로 접속.
- 작성일: 2026-06-24
- 한 번 셋업한 뒤에는 **8장 "재배포"** 만 반복하면 됨.

---

## 0. 사전 준비 (한 번만)

전역 기본값을 건드리지 않도록 **beavertalk 전용 구성(named configuration)** 을 만들어 그 안에서만 작업한다.
다른 GCP 프로젝트와 섞이지 않아 엉뚱한 곳에 배포할 사고를 막는다.

```bash
# gcloud CLI 설치돼 있다고 가정.
gcloud auth login

# 1) beavertalk 전용 구성 생성 (생성과 동시에 활성화됨)
gcloud config configurations create beavertalk

# 2) 이 구성에 계정·프로젝트·리전을 묶어서 저장
gcloud config set account hahahoho3797@gmail.com
gcloud config set project bt-dev-web-01   # 실제 프로젝트 ID
gcloud config set run/region asia-northeast3   # 기존 웹과 같은 리전 권장(아래에서 확인)

# 3) 기존 웹 서비스(beavertalk-api) 리전 확인 → 다르면 위 run/region 을 맞춰 다시 set
gcloud run services list

# 4) 필요한 API 활성화 (--source 빌드에 필요) — PowerShell은 한 줄
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
```

> `project`/`account`는 **GCP 프로젝트 ID·로그인 이메일**이어야 한다. 정확한 ID는 `gcloud projects list` 의 `PROJECT_ID` 열에서 확인.

**구성 전환/확인 (다른 작업하다 돌아올 때):**
```bash
gcloud config configurations list                 # 구성 목록 + 활성(*) 표시
gcloud config configurations activate beavertalk  # 이 작업으로 전환
gcloud config configurations activate default     # 원래(다른 작업)로 복귀
```

> 이후 **새 터미널을 열 때마다** beavertalk 작업이면 먼저 `activate beavertalk` 로 전환했는지 확인할 것.
> 활성 구성 빠르게 보기: `gcloud config configurations list` 또는 `gcloud config get-value project`.

---

## 1. Dockerfile 만들기

프로젝트 루트(`main.py` 옆)에 **`Dockerfile`** 생성:

```dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 의존성 먼저 복사 → 레이어 캐시로 빌드 빨라짐
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 코드 복사
COPY . .

# Cloud Run 은 $PORT(기본 8080)로 트래픽을 보낸다. 반드시 0.0.0.0 바인딩.
ENV PORT=8080
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
```

> 핵심: Cloud Run은 컨테이너가 **`$PORT`로 들어오는 요청을 0.0.0.0에서 받기**를 요구한다. 위 CMD가 그걸 보장.

---

## 2. .dockerignore 만들기

루트에 **`.dockerignore`** 생성 (이미지에서 불필요/민감 파일 제외):

```gitignore
# 가상환경 / 캐시
.venv/
venv/
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/

# VCS / 에디터 / 도구
.git/
.gitignore
.vscode/
.idea/
.claude/
.mcp.json

# 비밀값 / 로컬 설정 (절대 이미지에 넣지 않음)
.env
.env.*

# 런타임에 불필요
front/
docs/
tests/
*.md

# 대용량 생성물
api-docs.html
openapi.json
BeaverTalk.vuerd.json
```

> `.env`를 반드시 제외한다. 비밀값은 4장처럼 Secret Manager로 주입한다.

---

## 3. 비밀값을 Secret Manager에 저장

`.env`의 값을 이미지가 아니라 Secret Manager에 둔다. **아래는 Windows PowerShell 기준** (bash가 아님 — `printf`/`openssl`/줄끝 `\` 안 씀).

```powershell
# 1) 런타임 DB 연결(6543 풀러) — .env 의 DATABASE_URL_POOL 값을 직접 읽어 시크릿 생성
#    (값이 화면/기록에 남지 않도록 .env 에서 읽어 임시파일로만 전달)
$pool = ((Select-String -Path .env -Pattern '^DATABASE_URL_POOL=').Line -replace '^DATABASE_URL_POOL=','').Trim().Trim('"')
Set-Content -Path tmp_pool.txt -Value $pool -NoNewline -Encoding ascii
gcloud secrets create beavertalk-app-db-pool --replication-policy=automatic --data-file=tmp_pool.txt
Remove-Item tmp_pool.txt

# 2) JWT 서명 키 — 32바이트 무작위 hex (openssl 없이 PowerShell 내장 RNG)
$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$jwt = -join ($bytes | ForEach-Object { $_.ToString('x2') })
Set-Content -Path tmp_jwt.txt -Value $jwt -NoNewline -Encoding ascii
gcloud secrets create beavertalk-app-jwt-secret --replication-policy=automatic --data-file=tmp_jwt.txt
Remove-Item tmp_jwt.txt
```

> 이미 만든 시크릿이라 `create`가 "already exists"로 실패하면, 값만 새 버전으로 추가:
> `gcloud secrets versions add beavertalk-app-db-pool --data-file=tmp_pool.txt`

Cloud Run 런타임 서비스 계정에 **읽기 권한** 부여 (PowerShell):

```powershell
$PNUM = (gcloud projects describe bt-dev-web-01 --format='value(projectNumber)').Trim()
$SA = "serviceAccount:${PNUM}-compute@developer.gserviceaccount.com"

gcloud secrets add-iam-policy-binding beavertalk-app-db-pool --member="$SA" --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding beavertalk-app-jwt-secret --member="$SA" --role="roles/secretmanager.secretAccessor"
```

> 선택 비밀값(있으면 같은 방식으로 추가): `RESEND_API_KEY`, `MAIL_FROM`, `GOOGLE_CLIENT_ID`, `SPEECH_SUPER_APP_KEY`, `SPEECH_SUPER_SECRET_KEY`. 없으면 해당 기능은 스텁/콘솔 폴백으로 동작.

---

## 4. DB 마이그레이션 (배포 전에 1번)

Cloud Run 인스턴스에서 돌리지 말고 **로컬에서 직접 연결(5432, DIRECT)** 로 실행한다.
`.env`에 `DATABASE_URL_DIRECT`(5432)가 있어야 한다.

```bash
# 로컬 가상환경(예: conda beavertalk-server)에서
alembic upgrade head
alembic current   # 적용 버전 확인
```

> 스키마를 바꾼 배포에서는 매번 이 단계를 먼저 한다. (코드 모델만 바뀌고 마이그레이션 안 돌리면 `column does not exist` 에러)

---

## 5. 배포 (소스에서 바로 빌드 + 배포)

루트에서 한 줄. `--source .` 가 Dockerfile로 빌드→Artifact Registry 푸시→배포까지 한다.

**PowerShell은 한 줄로** (줄 끝 `\` 안 됨 — 백틱 `` ` `` 이거나 한 줄):

```powershell
gcloud run deploy beavertalk-app-api --source . --region asia-northeast3 --allow-unauthenticated --set-env-vars ENV=prod --set-secrets "DATABASE_URL_POOL=beavertalk-app-db-pool:latest,JWT_SECRET=beavertalk-app-jwt-secret:latest"
```

- 첫 실행 때 빌드용 저장소(`cloud-run-source-deploy`) 생성 여부를 물으면 **Y**.
- `--allow-unauthenticated`: 공개 API(앱이 호출)라 인증 없이 접근 허용. 내부 전용이면 빼기.
- 끝나면 출력에 **Service URL**(`https://beavertalk-app-api-xxxxx.run.app`)이 나온다.

---

## 6. 동작 확인

PowerShell:
```powershell
$URL = (gcloud run services describe beavertalk-app-api --region asia-northeast3 --format='value(status.url)').Trim()
Invoke-RestMethod "$URL/health"        # status=ok, env=prod 기대
```

- 브라우저로 `"$URL/docs"` 열어 Swagger 확인.
- `/__console`(dev 테스트 콘솔)은 **prod에서 자동으로 숨겨짐** — 정상.

---

## 7. CORS / 프론트 연결

- 현재 `main.py`는 `allow_origins=["*"]`, `allow_credentials=False`라 토큰(Bearer) 방식 호출은 그대로 동작한다.
- 만약 **쿠키 기반 인증**으로 바꾸면, `allow_origins`를 실제 웹/앱 도메인으로 좁히고 `allow_credentials=True`로 바꿔야 한다(둘 다 `*`는 불가). 지금은 Bearer 토큰이라 손댈 것 없음.

---

## 8. 재배포 (코드 바꿀 때마다)

> 배포는 **맨 마지막 한 번**이면 된다. 2번(시크릿 새 버전)은 **.env 비밀값이 바뀐 경우에만** 하는 선택 단계.

```powershell
# 0) beavertalk 구성으로 전환됐는지 먼저 확인 (다른 작업하다 왔다면 필수)
gcloud config configurations activate beavertalk

# 1) (스키마 변경 있었으면) 마이그레이션 먼저
alembic upgrade head

# 2) (.env 비밀값이 바뀐 경우에만) Secret Manager 에 새 버전 추가
#    값이 그대로면 이 단계는 통째로 건너뛴다.
$pool = ((Select-String -Path .env -Pattern '^DATABASE_URL_POOL=').Line -replace '^DATABASE_URL_POOL=','').Trim().Trim('"')
Set-Content tmp_pool.txt -Value $pool -NoNewline -Encoding ascii
gcloud secrets versions add beavertalk-app-db-pool --data-file=tmp_pool.txt
Remove-Item tmp_pool.txt

# 3) 배포 (항상, 한 줄) — :latest 는 이 시점에 다시 읽힌다
gcloud run deploy beavertalk-app-api --source . --set-env-vars ENV=prod --set-secrets "DATABASE_URL_POOL=beavertalk-app-db-pool:latest,JWT_SECRET=beavertalk-app-jwt-secret:latest"
```


---

## 9. 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| 배포는 됐는데 컨테이너가 안 뜸 (startup 실패) | `ENV=prod`인데 `JWT_SECRET`이 dev 기본값/미설정 → Secret 주입 확인(3·5장) |
| `/health`는 OK인데 쿼리에서 500 | DB URL 오류 → `DATABASE_URL_POOL` 시크릿 값(특히 비번 특수문자 `@`→`%40`) 확인 |
| `column ... does not exist` | 마이그레이션 미적용 → 4장 `alembic upgrade head` |
| 로그 보기 | `gcloud run services logs read beavertalk-app-api --limit 50` |
| 포트 관련 startup 타임아웃 | Dockerfile CMD가 `--host 0.0.0.0 --port ${PORT}` 인지 확인(1장) |

---

## 부록 — 최종 체크리스트

- [ ] 0장: gcloud 로그인 + **beavertalk 전용 구성 생성/활성화** + 프로젝트·리전 설정 + API 활성화
- [ ] 1장: `Dockerfile` 생성
- [ ] 2장: `.dockerignore` 생성
- [ ] 3장: Secret 2개 생성 + SA 권한 부여
- [ ] 4장: `alembic upgrade head`
- [ ] 5장: `gcloud run deploy ... --source .`
- [ ] 6장: `/health`, `/docs` 확인
