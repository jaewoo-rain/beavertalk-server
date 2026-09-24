"""옛 3티어 plan 값 정규화 — R3-b(2026-09-24, bt-back).

⛔ 왜 별도 파일인가: `test_plan_two_tier.py::test_no_bare_pro_max_plan_literals_
in_the_plan_carrying_files` 가 plan 을 다루는 핵심 파일들(`subscription_status.py`
포함)에서 옛 리터럴 `"pro"`/`"max"` 자체를 금지한다 — 정규화 로직이 있어야 할
바로 그 자리가 그 리터럴을 못 쓴다. 그래서 이 상수만 따로 뗐다(그 시험의 스캔
목록에 없다).

배경: premium 브랜치(D1 2단화)의 plan 해석표는 `None`/`"premium"` 두 키뿐이다
(`call_service.py` 의 `CALL_*_BY_PLAN`·`DAILY_BUDGET_S_BY_PLAN`, `entitlements.py`
의 `PLANS_UNLOCKING_ALL_CHARACTERS`) — 모르는 값은 전부 Free 로 떨어진다(R5).
그런데 **DB 는 공유**다 — 지금 배포된 app-api 코드는 아직 `plan='pro'|'max'` 를
쓴다(`b068a7dedfd3` 는 1회성 UPDATE 뿐 CHECK 제약이 없고, downgrade 는 전 행을
`'max'` 로 되돌린다). 그 값이 이 브랜치로 넘어오면 **결제한 pro/max 회원이 조용히
Free 로 떨어진다**(영상 False·조각 1·예산 300초·캐릭터 잠김) — R5 의 "모르는 값은
안전하게 Free" 원칙이 **아직 유효한 유료 고객**에게 잘못 적용되는 사고다.

`subscription_status.resolve_status`(→ `_from_row`) 가 plan 을 만드는 **단 하나의
지점**이라(entitlements.effective_plan·SubscriptionService.status 둘 다 그 결과를
그대로 쓴다, grep 으로 확인 — 다른 소비자 없음) 거기서 한 번 접으면 하류 전부가
정규화된 값을 본다. 진짜 모르는 값(예 'xyz', DB 오염)은 여기 없으니 그대로
통과해 하류의 Free 폴백이 정상 작동한다 — pro/max 만 표적이다.
"""

from __future__ import annotations

LEGACY_PLAN_TO_TWO_TIER: dict[str, str] = {
    "pro": "premium",
    "max": "premium",
}


def normalize_plan(plan: str) -> str:
    """옛 3티어 값이면 2단화 값으로 접는다. 그 외(이미 정규화됐거나 진짜 모르는
    값)는 그대로 돌려준다 — 하류(call_service 표)의 Free 폴백을 건드리지 않는다."""
    return LEGACY_PLAN_TO_TWO_TIER.get(plan, plan)
