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
# ⭐ 대상 서비스를 env 로 고를 수 있다(2026-08-20). 기본은 demo 그대로다.
#   ⚠ 사장님이 자기 개발용으로 app-api 를 쓰신다 — 그쪽에 수동 배포할 일이 생겼다.
#   ⛔ 스크립트를 복제하지 않는다. 두 벌이 되면 한쪽만 고쳐지고, 이 파일에 적힌 함정
#     (멀티매니페스트 회피·--source 실패)이 다른 쪽에서 되살아난다.
#   예) SERVICE=beavertalk-app-api scripts/deploy_demo.sh v2
SERVICE="${SERVICE:-beavertalk-app-demo-api}"
BASE="https://${SERVICE}-333511894671.asia-northeast3.run.app"
TAG="${1:-demo-$(date +%s)}"
IMG="asia-northeast3-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/${SERVICE}:${TAG}"

echo "[1/3] 빌드: ${TAG}"
gcloud builds submit --region "$REGION" --project "$PROJECT" --tag "$IMG" --quiet

echo "[2/3] 배포"
# --timeout=3600: Cloud Run 요청 타임아웃(기본 300s=5분)은 WS 통화에도 걸려 5분 통화가
#   작별 직전 소켓째 끊긴다(실측 call 195: 298s 문장 중간 절단). 통화 WS 는 하나의 긴 요청이므로
#   상한을 최대(3600s=60분)로 올려 서버 시계(종료 시드·작별)가 온전히 돌게 한다.
gcloud run deploy "$SERVICE" --image "$IMG" --region "$REGION" --project "$PROJECT" --timeout=3600 --quiet

echo "[3/3] 헬스체크"
printf 'health:%s  ' "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health")"
printf 'leveldemo:%s  ' "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/__levelcalldemo")"
printf 'calldemo:%s\n' "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/__calldemo")"
echo "URL: $BASE"
