"""B2B 교실 서비스에 묻는 클라이언트.

교실·과제 도메인은 2026-09-02 결정으로 별도 서비스(`beavertalk-b2b-api`)로
분리됐다. 이 서버는 그 테이블을 읽지 않는다 — 같은 DB 라 기술적으로는 되지만,
읽기 시작하면 **목표 산정 로직이 두 저장소로 갈린다.** 교사 콘솔이 센 목표 수와
학습자가 통화에서 받는 목표가 달라지는 순간 「10개라며 왜 7개만 나오나」가 된다.

## 이 파일의 규율 하나

**실패는 전부 빈 결과다.** 회화 목표는 통화의 **성립 조건이 아니라 재료**다.
B2B 가 죽었다고 통화를 막으면, 숙제와 무관한 학습자까지 전화를 못 건다.
그래서 타임아웃·연결 실패·4xx·5xx·파싱 실패를 모두 삼키고 `[]` 를 준다
(호출부는 평소 선별로 되돌아간다).

⚠ 그래서 **조용히 안 될 수 있다.** 설정이 빠진 것과 B2B 가 죽은 것을 구분하려면
   로그를 봐야 한다 — 둘 다 WARNING 으로 남긴다.
"""

from __future__ import annotations

import logging
import json
import time
import ssl
import os

import certifi

import httpx

# TLS 신뢰 저장소 준비는 각 복구 요청의 잔여시간 밖에서 한 번만 수행한다.
_RESULT_SSL_CONTEXT = ssl.create_default_context(
    cafile=os.environ.get("SSL_CERT_FILE") or certifi.where(),
    capath=os.environ.get("SSL_CERT_DIR"),
)

from core.config import settings

logger = logging.getLogger(__name__)

# 통화 시작 경로에 끼어드는 호출이다. 학습자는 이 시간만큼 신호음을 더 듣는다.
# ⛔ 늘리지 마라 — 늦게 오는 목표보다 제때 걸리는 전화가 낫다.
_TIMEOUT = httpx.Timeout(connect=1.0, read=2.0, write=2.0, pool=1.0)

#: 교실 커리큘럼의 언어. B2B 는 한국어 교육기관용이고, 저쪽 `CURRICULUM_LANGUAGE`
#: 와 같은 값이어야 한다 — 다르면 저쪽이 목표를 빈 배열로 돌려준다.
#:
#: 🔴 **과제 통화의 언어이기도 하다**(`call_session.run_call`). 학습자가 앱을 다른
#:    언어 학습으로 두고 있어도 교사가 낸 과제는 한국어다.
CURRICULUM_LANGUAGE = "ko"


class HomeworkMaterialsError(Exception):
    """숙제 전용 조회 실패. 개인 통화 자료로 대체하지 않는다."""

    def __init__(self, code: str, *, recoverable: bool = False):
        super().__init__(code)
        self.code = code
        self.recoverable = recoverable


def conversation_result(member_id: int, assignment_id: int, call_id: int,
                        *, deadline: float | None = None) -> dict:
    """저장 증거는 B2B가 읽는다. 요청에는 call_id만 전달한다."""
    base = (settings.B2B_API_BASE_URL or "").strip().rstrip("/")
    token = (settings.B2B_SERVICE_TOKEN or "").strip()
    if not base or not token:
        raise HomeworkMaterialsError("homework_service_unavailable", recoverable=True)
    timeout = _TIMEOUT
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HomeworkMaterialsError("homework_service_unavailable", recoverable=True)
        timeout = httpx.Timeout(max(0.001, remaining / 4))
    try:
        with httpx.Client(timeout=timeout, verify=_RESULT_SSL_CONTEXT) as client:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HomeworkMaterialsError("homework_service_unavailable", recoverable=True)
                client.timeout = httpx.Timeout(max(0.001, remaining / 4))
            url = f"{base}/api/v1/internal/members/{member_id}/assignments/{assignment_id}/conversation-result"
            with client.stream("POST", url, headers={"X-Service-Token": token}, json={"call_id": call_id}) as response:
                content = bytearray()
                for chunk in response.iter_bytes():
                    if deadline is not None and time.monotonic() >= deadline:
                        raise HomeworkMaterialsError("homework_service_unavailable", recoverable=True)
                    content.extend(chunk)
                    if len(content) > 65536:
                        raise HomeworkMaterialsError("homework_result_invalid")
    except httpx.RequestError:
        raise HomeworkMaterialsError("homework_service_unavailable", recoverable=True) from None
    if response.status_code >= 500:
        raise HomeworkMaterialsError("homework_service_unavailable", recoverable=True)
    try:
        payload = json.loads(content)
    except ValueError:
        raise HomeworkMaterialsError("homework_result_invalid") from None
    if response.status_code != 200:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        retryable = {"call_not_finished", "homework_analysis_pending", "homework_call_active",
                     "homework_analysis_stale", "homework_not_final", "assignment_not_published"}
        known = retryable | {"Unauthorized", "Not Found", "assignment_not_found", "call_not_found",
                             "call_owner_mismatch", "homework_binding_mismatch", "homework_snapshot_invalid",
                             "homework_verification_unsupported", "homework_proof_invalid",
                             "homework_analysis_failed", "homework_kind_blocked", "conversation_already_linked",
                             "assignment_closed", "conversation_not_enabled"}
        code = detail if isinstance(detail, str) and detail in known else "homework_result_invalid"
        raise HomeworkMaterialsError(code, recoverable=code in retryable)
    if (not isinstance(payload, dict) or payload.get("assignment_id") != assignment_id
            or payload.get("call_id") != call_id
            or payload.get("outcome") not in {"linked", "already_linked", "not_performed"}
            or type(payload.get("performed")) is not bool
            or type(payload.get("met")) is not int or type(payload.get("total")) is not int
            or not 0 <= payload["met"] <= payload["total"] <= 10 or payload["total"] < 1
            or payload.get("submission_status") not in {"not_started", "in_progress", "done"}):
        raise HomeworkMaterialsError("homework_result_invalid")
    return payload


def conversation_materials(
    member_id: int, *, assignment_id: int, locale: str,
) -> dict:
    """B2B가 승인·선정한 source별 원문. 구 goals 조회의 폴백은 변경하지 않는다."""
    from core.prompts.homework import normalize_homework

    base = (settings.B2B_API_BASE_URL or "").strip().rstrip("/")
    token = (settings.B2B_SERVICE_TOKEN or "").strip()
    if not base or not token:
        raise HomeworkMaterialsError("homework_service_unavailable", recoverable=True)
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            res = client.get(
                f"{base}/api/v1/internal/members/{member_id}/conversation-materials",
                params={"assignment_id": assignment_id, "language": "ko", "locale": locale},
                headers={"X-Service-Token": token},
            )
    except httpx.RequestError:
        # 토큰·주소를 포함할 수 있는 외부 예외 전문은 기록하지 않는다.
        logger.warning("b2b: 숙제 자료 연결 실패")
        raise HomeworkMaterialsError("homework_service_unavailable", recoverable=True) from None
    if res.status_code >= 500:
        raise HomeworkMaterialsError("homework_service_unavailable", recoverable=True)
    try:
        payload = res.json()
    except ValueError:
        raise HomeworkMaterialsError("invalid_assignment_materials") from None
    if res.status_code != 200:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        allowed = {
            "assignment_not_found", "language_mismatch", "assignment_not_published",
            "assignment_closed", "conversation_completed", "conversation_not_enabled",
            "invalid_assignment_materials", "empty_conversation_materials",
        }
        code = detail if isinstance(detail, str) and detail in allowed else "homework_service_unavailable"
        raise HomeworkMaterialsError(code, recoverable=code == "homework_service_unavailable")
    try:
        data = normalize_homework(payload)
        if data["assignment_id"] != assignment_id:
            raise ValueError("숙제 식별 불일치")
    except ValueError:
        raise HomeworkMaterialsError("invalid_assignment_materials") from None
    return data


def conversation_goal_item_ids(
    member_id: int,
    *,
    assignment_id: int | None = None,
    language: str = CURRICULUM_LANGUAGE,
) -> list[int]:
    """이 학습자가 지금 통화에서 써야 할 목표 항목 id.

    `assignment_id` 를 주면 그 과제로 좁힌다(숙제 상세에서 시작한 통화). 안 주면
    B2B 가 참여 중인 반의 열린 과제를 합쳐 준다.

    자격 검증(명단원인가·닫힌 과제인가·언어가 맞는가)은 **전부 저쪽이 한다.**
    남의 과제 id 를 들고 와도 빈 배열이 온다 — 여기서 다시 판단하지 않는다.

    Returns:
        항목 id 목록. 설정이 없거나 호출이 실패하면 **빈 목록**이다.
    """
    base = (settings.B2B_API_BASE_URL or "").strip().rstrip("/")
    token = (settings.B2B_SERVICE_TOKEN or "").strip()
    if not base or not token:
        # 설정이 없으면 숙제 통화가 평소 통화와 같아진다. 조용히 지나가되 남긴다.
        logger.warning(
            "b2b: 회화 목표 조회 설정 없음(B2B_API_BASE_URL·B2B_SERVICE_TOKEN) — 평소 선별로 진행"
        )
        return []

    params: dict[str, object] = {"language": language}
    if assignment_id is not None:
        params["assignment_id"] = assignment_id

    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            res = client.get(
                f"{base}/api/v1/internal/members/{member_id}/conversation-goals",
                params=params,
                headers={"X-Service-Token": token},
            )
        res.raise_for_status()
        payload = res.json()
    except Exception:  # noqa: BLE001 - 통화를 막지 않는 것이 이 함수의 계약이다
        logger.warning("b2b: 회화 목표 조회 실패 — 평소 선별로 진행", exc_info=True)
        return []

    raw = payload.get("item_ids") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    return [int(i) for i in raw if isinstance(i, (int, str)) and str(i).lstrip("-").isdigit()]
