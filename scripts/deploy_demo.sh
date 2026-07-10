#!/usr/bin/env bash
# demo-api(beavertalk-app-demo-api) 배포 헬퍼 — dev 전용.
#
# 왜 이 스크립트인가:
#   - `gcloud run deploy --source .` 는 :latest 태그가 멀티매니페스트(이미지+증명)를
#     가리켜 Cloud Run 이 "Container import failed" 로 거부한다.
#   - 그래서 `builds submit --tag`(클래식 단일 매니페스트)로 빌드 → 그 태그로 deploy.
#
# 사용법:
#   scripts/deploy_demo.sh              # 태그 자동(demo-<epoch>)
#   scripts/deploy_demo.sh p27          # 태그 지정
set -euo pipefail

PROJECT=bt-dev-web-01
REGION=asia-northeast3
SERVICE=beavertalk-app-demo-api
BASE=https://beavertalk-app-demo-api-333511894671.asia-northeast3.run.app
TAG="${1:-demo-$(date +%s)}"
IMG="asia-northeast3-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/${SERVICE}:${TAG}"

echo "[1/3] 빌드: ${TAG}"
gcloud builds submit --region "$REGION" --project "$PROJECT" --tag "$IMG" --quiet

echo "[2/3] 배포"
gcloud run deploy "$SERVICE" --image "$IMG" --region "$REGION" --project "$PROJECT" --quiet

echo "[3/3] 헬스체크"
printf 'health:%s  ' "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health")"
printf 'leveldemo:%s  ' "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/__levelcalldemo")"
printf 'calldemo:%s\n' "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/__calldemo")"
echo "URL: $BASE"
