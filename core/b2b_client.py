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

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

# 통화 시작 경로에 끼어드는 호출이다. 학습자는 이 시간만큼 신호음을 더 듣는다.
# ⛔ 늘리지 마라 — 늦게 오는 목표보다 제때 걸리는 전화가 낫다.
_TIMEOUT = httpx.Timeout(connect=1.0, read=2.0, write=2.0, pool=1.0)


def conversation_goal_item_ids(
    member_id: int, *, assignment_id: int | None = None, language: str = "ko"
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
