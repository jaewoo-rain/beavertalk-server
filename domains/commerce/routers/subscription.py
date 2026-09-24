"""subscription 라우터 — 구독 시작/목록/취소."""

from __future__ import annotations

from fastapi import APIRouter, status

from core.deps import CurrentAdmin, CurrentMember, DbSession
from domains.commerce.schemas.subscription import (
    SubscribeCreate,
    SubscriptionOut,
    SubscriptionStatusOut,
)
from domains.commerce.service.subscription_service import SubscriptionService

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.post("", response_model=SubscriptionOut, status_code=status.HTTP_201_CREATED)
def start_subscription(
    data: SubscribeCreate, member: CurrentAdmin, db: DbSession
) -> SubscriptionOut:
    """⛔⛔ R3-a(2026-09-24, bt-back — 판매 개시 전 필수) — **admin 전용**으로 좁혔다.
    금액 검증이 `gt=0` 뿐이라(클라가 가격을 스스로 정한다) 아무 회원이나 이 API 를
    한 번 불러 본인 JWT 로 `is_activate=True, plan="premium"` 행을 즉시 만들 수
    있었다 — 실결제 없이 영상·15분·전 캐릭터를 스스로 지급하는 구멍.

    ⚠ 이 API 자체는 **폐기하지 않는다**(IAP 전환 전 사장님 개발 통로,
    `SubscriptionService.start` 문서 참조) — admin(`role=="admin"`)만 통과시킨다.
    admin 이 아닌 회원의 진짜 구독은 이 경로를 안 쓴다(`POST /purchases/verify`,
    `iap_service.verify_and_grant` — 그쪽은 건드리지 않았다).
    """
    return SubscriptionService(db).start(member.member_id, data)


@router.get("", response_model=list[SubscriptionOut])
def list_subscriptions(member: CurrentMember, db: DbSession) -> list[SubscriptionOut]:
    """내 구독 목록(활성/만료 포함)."""
    return SubscriptionService(db).list(member.member_id)


@router.get("/status", response_model=SubscriptionStatusOut)
def get_subscription_status(
    member: CurrentMember, db: DbSession
) -> SubscriptionStatusOut:
    """내 **현재 구독 상태** 1건 — 상태 8종 + 플랜.

    목록(`GET /subscriptions`)이 행 이력이라면 이건 "지금 어디에 있나"의 단일 답이다.
    앱이 행 목록에서 상태를 역추론하면 해지 안내가 틀어지므로 판정을 서버가 소유한다.

    ⚠ 라우트 순서: `/{subscribe_id}` 형태의 경로 매개변수 라우트보다 **위**에 있어야
      "status" 가 id 로 먹히지 않는다(지금은 그런 라우트가 없지만 추가될 때를 대비).
    """
    return SubscriptionService(db).status(member.member_id)


@router.post("/{subscribe_id}/cancel", response_model=SubscriptionOut)
def cancel_subscription(
    subscribe_id: int, member: CurrentMember, db: DbSession
) -> SubscriptionOut:
    """구독 취소(해지) — 해당 구독을 비활성화한다."""
    return SubscriptionService(db).cancel(member.member_id, subscribe_id)
