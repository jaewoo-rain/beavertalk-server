#!/usr/bin/env bash
# app-api(beavertalk-app-api) 배포 — ⚠ **실서비스다.**
#
# ⛔⛔ 이 스크립트는 **사장님 지시가 있을 때만** 돈다(CLAUDE.md R6).
#
# 왜 demo 와 스크립트를 갈랐나 (2026-08-16):
#   실서비스가 **demo 이미지**(`beavertalk-app-demo-api:demo-1785885788`)를 쓰고 있었다.
#   demo 를 배포하다 실수로 app-api 에 붙이면 **캐스케이드가 실서비스에 켜지는** 구조였다.
#   ⇒ 이미지 리포지토리 이름부터 갈라, 그 사고가 **구조적으로** 안 나게 한다.
#   ⚠ 그래도 `CASCADE_ENABLED` 는 app-api 에 미설정이라 캐스케이드는 꺼져 있다 —
#     이미지 분리는 그 방어를 한 겹 더 두는 것이지, 지금 켜져 있다는 뜻이 아니다.
#
# ⚠ demo 와 다른 점:
#   - 태그 접두사 `prod-` (demo 는 `demo-`) — 로그·콘솔에서 눈으로 갈린다
#   - 이미지 이름이 `beavertalk-app-api` (demo 는 `beavertalk-app-demo-api`)
#   - 배포 전 **브랜치 확인**을 강제한다(기본 main). 아무 브랜치나 실서비스에 못 올린다
#
# ⛔ 마이그레이션은 이 스크립트가 **안 돌린다.** 스키마가 바뀌었으면 배포 **전에**
#    `alembic upgrade head` 를 직결(5432)로 따로 돌려라 — 롤백이 필요할 때 코드와
#    스키마를 따로 되돌릴 수 있어야 한다.
#
# 사용법:
#   scripts/deploy_prod.sh              # 태그 자동(prod-<epoch>), main 만 허용
#   scripts/deploy_prod.sh p3           # 태그 지정
#   ALLOW_BRANCH=hotfix/x scripts/deploy_prod.sh   # 예외적으로 다른 브랜치 허용
set -euo pipefail

PROJECT=bt-dev-web-01
REGION=asia-northeast3
SERVICE=beavertalk-app-api
BASE=https://beavertalk-app-api-333511894671.asia-northeast3.run.app
TAG="${1:-prod-$(date +%s)}"
IMG="asia-northeast3-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/${SERVICE}:${TAG}"
WANT="${ALLOW_BRANCH:-main}"

CUR="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CUR" != "$WANT" ]; then
  echo "⛔ 실서비스 배포는 '$WANT' 에서만 한다 (지금: '$CUR')." >&2
  echo "   다른 브랜치를 올리려면 ALLOW_BRANCH=<이름> 을 명시해라." >&2
  exit 1
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "⛔ 커밋 안 된 변경이 있다. 실서비스에 올라가는 것과 저장소가 어긋난다." >&2
  git status --short --untracked-files=no >&2
  exit 1
fi

echo "⚠ 실서비스 배포: $SERVICE"
echo "   브랜치 $CUR / 커밋 $(git rev-parse --short HEAD) / 태그 $TAG"

echo "[1/3] 빌드: ${TAG}"
gcloud builds submit --region "$REGION" --project "$PROJECT" --tag "$IMG" --quiet

echo "[2/3] 배포"
# --timeout=3600: 통화 WS 는 하나의 긴 요청이다. 기본 300s 면 5분 통화가 작별 직전 끊긴다
#   (실측 call 195: 298s 문장 중간 절단). demo 와 같은 이유로 상한을 올린다.
gcloud run deploy "$SERVICE" --image "$IMG" --region "$REGION" --project "$PROJECT" --timeout=3600 --quiet

echo "[3/3] 헬스체크"
printf 'health:%s\n' "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health")"
echo "URL: $BASE"
echo "리비전: $(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.latestReadyRevisionName)')"
