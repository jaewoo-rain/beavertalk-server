"""구독이 열어주는 혜택(entitlement) 판정 — 플랜 → 무엇이 가능한가.

⛔ **소유(ownership)와 접근(entitlement)은 다른 축이다.**

    소유   member_character 행. 돈 주고 산 것. **영구**다.
    접근   구독이 여는 것. 구독이 끝나면 **닫힌다**.

Max 의 "모든 캐릭터 무제한"은 후자다. 그래서 Max 회원에게 member_character 행을
만들어 주면 안 된다 — 해지 후에도 영구 소유가 되어 되돌릴 수 없다. 접근은 DB 에
쓰지 않고 **읽는 시점에 파생 계산**한다(체크판의 "증거가 원본, 나머지는 파생"과 같은 규율).

앱도 두 축을 구분한다: `downgradeWarning` = *"Video calls and Max-only characters
turn off on {date}"* — 해지하면 잠긴다는 걸 화면이 이미 말하고 있다. 그래서 wire 에서도
`is_owned`(영구 구매)와 `is_unlocked`(지금 쓸 수 있음)를 섞지 않는다.

왜 commerce 인가: 판정 재료가 구독 행이다. learning 이 commerce 를 참조하는 건 맞고
그 역은 아니다 — call_service 는 여기로 위임한다.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 이 플랜은 카탈로그 전체를 연다. 구매 없이 **쓸 수** 있다는 뜻이지 소유가 아니다.
PLANS_UNLOCKING_ALL_CHARACTERS = frozenset({"max"})

# 혜택이 실제로 열려 있는 구독 상태. grace(결제 재시도 중)·ending(해지했지만 기간 남음)이
# 포함되는 게 요점이다 — 카드가 한 번 실패했다고 캐릭터가 잠기면, 갱신하는 며칠 동안
# 결제한 사람이 서비스를 못 쓴다. on_hold(유예도 끝남)·expired 는 열지 않는다.
_ACTIVE_STATES = frozenset({"trial", "active_pro", "active_max", "grace", "ending"})


def effective_plan(db: Session, member_id: int) -> Optional[str]:
    """지금 **실제로 혜택이 열려 있는** 플랜. Free 면 None.

    ⛔ 판정 규칙을 새로 쓰지 않고 resolve_status 를 재사용한다 — 두 곳이 어긋나면
      "앱은 되는데 서버가 거절"이 된다. 특히 grace(접근 유지)와 on_hold(접근 차단)의
      비대칭이 그렇다.

    실패는 Free 로 떨어뜨린다(R5): 구독 조회가 통화를 막으면 안 되고, 모르면
    보수적으로 무료 혜택을 적용하는 편이 과금 사고보다 낫다.
    """
    try:
        from domains.commerce.repository.subscribe_repository import SubscribeRepository
        from domains.commerce.service.subscription_status import resolve_status

        resolved = resolve_status(SubscribeRepository(db).list_by_member(member_id))
    except Exception:  # noqa: BLE001 - 구독 조회 실패가 서비스를 막으면 안 된다
        logger.warning("entitlements: 플랜 판정 실패 → Free 로 처리 member=%s", member_id)
        return None
    return resolved.plan if resolved.state in _ACTIVE_STATES else None


def unlocks_all_characters(plan: Optional[str]) -> bool:
    """이 플랜이 카탈로그 전체를 여는가(Max 전용 혜택)."""
    return plan in PLANS_UNLOCKING_ALL_CHARACTERS


def has_all_characters(db: Session, member_id: int) -> bool:
    """이 회원이 지금 모든 캐릭터를 쓸 수 있는가(구독 기준 — 소유와 무관)."""
    return unlocks_all_characters(effective_plan(db, member_id))
