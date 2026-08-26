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

# ⭐ 선택: 배포하면서 환경변수를 얹는다(2026-08-20).
#   예) EXTRA_ENV=LIVE_FACE_SPIKE=true SERVICE=beavertalk-app-api scripts/deploy_demo.sh v3
#   ⛔⛔ **`--set-env-vars` 를 쓰지 마라 — 기존 env 를 통째로 갈아치운다.**
#     이 서비스의 env 에는 DB·Gemini·Vertex 설정이 들어 있어서, 한 번 날리면 통화가
#     전부 죽고 무엇이 있었는지도 남지 않는다. `--update-env-vars` 는 **병합**이다.
#   ⚠ 시크릿(--set-secrets)은 별개 축이라 이 인자가 안 건드린다.
ENV_ARGS=()
if [[ -n "${EXTRA_ENV:-}" ]]; then
  echo "  env 추가(병합): ${EXTRA_ENV}"
  ENV_ARGS=(--update-env-vars "${EXTRA_ENV}")
fi

# ⭐ 선택: 배포하면서 환경변수를 **지운다**(2026-08-27).
#   예) REMOVE_ENV=LIVE_FACE_TOOL_SCHEDULING SERVICE=beavertalk-app-api scripts/deploy_demo.sh v4
#   ⛔ 왜 필요한가 — 코드에서 설정을 없앨 때 **env 는 따로 안 지워진다.** `extra="ignore"`
#     라 앱은 안 죽지만, 다음 사람이 `describe` 로 보면 여전히 살아 있는 것처럼 보인다.
#     실제로 `LIVE_FACE_TOOL_SCHEDULING=SILENT` 이 그렇게 남아 사고를 냈다.
#   ⚠ `--update-env-vars` 와 같이 쓸 수 있다(축이 다르다). 지우는 쪽이 우선이라
#     같은 키를 양쪽에 넣지 마라.
if [[ -n "${REMOVE_ENV:-}" ]]; then
  echo "  env 삭제: ${REMOVE_ENV}"
  ENV_ARGS+=(--remove-env-vars "${REMOVE_ENV}")
fi

echo "[2/3] 배포"
# --timeout=3600: Cloud Run 요청 타임아웃(기본 300s=5분)은 WS 통화에도 걸려 5분 통화가
#   작별 직전 소켓째 끊긴다(실측 call 195: 298s 문장 중간 절단). 통화 WS 는 하나의 긴 요청이므로
#   상한을 최대(3600s=60분)로 올려 서버 시계(종료 시드·작별)가 온전히 돌게 한다.
gcloud run deploy "$SERVICE" --image "$IMG" --region "$REGION" --project "$PROJECT"   --timeout=3600 "${ENV_ARGS[@]}" --quiet

echo "[3/3] 헬스체크"
printf 'health:%s  ' "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health")"
printf 'leveldemo:%s  ' "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/__levelcalldemo")"
printf 'calldemo:%s\n' "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/__calldemo")"
echo "URL: $BASE"
