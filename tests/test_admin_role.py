"""관리자 롤 + 구매 가격 경합 방어 회귀.

- /__dev/* 운영 도구는 ENV 게이트만으로 가려져 있었는데 실서비스조차 ENV="test" 라
  사실상 로그인한 아무 회원에게나 열려 있었다 → member.role == "admin" 으로 막는다.
- 한정 할인이 "구매" 탭과 서버 처리 사이에 끝나면 화면가($5)와 청구가($10)가 어긋난다
  → 클라가 본 가격(expected_price)을 대조해 다르면 409.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from core.deps import ADMIN_ROLE, get_current_admin
from domains.commerce.schemas.purchase import PurchaseRequest


class _M:
    def __init__(self, role):
        self.member_id = 1
        self.role = role


def test_admin_passes():
    m = _M(ADMIN_ROLE)
    assert get_current_admin(m) is m


@pytest.mark.parametrize("role", ["user", "", "Admin", "superuser"])
def test_non_admin_is_403(role):
    """대소문자·유사 문자열도 통과하면 안 된다 — 정확히 "admin" 만."""
    with pytest.raises(HTTPException) as ex:
        get_current_admin(_M(role))
    assert ex.value.status_code == 403
    assert ex.value.detail["code"] == "ADMIN_ONLY"


def test_missing_role_attribute_is_403():
    """role 이 없는 옛 객체가 흘러들어도 열리면 안 된다(기본 거부)."""
    class _Old:
        member_id = 1

    with pytest.raises(HTTPException) as ex:
        get_current_admin(_Old())
    assert ex.value.status_code == 403


def test_expected_price_is_optional():
    """구버전 앱은 안 보낸다 — 없으면 검사를 건너뛴다(하위호환)."""
    assert PurchaseRequest().expected_price is None
    assert PurchaseRequest(expected_price=Decimal("5.00")).expected_price == Decimal("5.00")


def test_price_mismatch_raises_409():
    """할인 종료 직후: 클라가 본 $5 != 서버 계산 $10 → 409 로 거절하고 실제가를 알려준다."""
    from domains.commerce.service.purchase_service import PurchaseService

    svc = PurchaseService.__new__(PurchaseService)

    class _Char:
        character_id, name, price = 2, "BIBI", Decimal("10.00")
        discount_events: list = []

    class _CharRepo:
        def get(self, _cid): return _Char()

    class _McRepo:
        def get(self, _m, _c): return None

    class _CharSvc:
        def effective_price(self, _c): return Decimal("10.00")

    svc.char_repo, svc.mc_repo, svc.char_service = _CharRepo(), _McRepo(), _CharSvc()

    with pytest.raises(HTTPException) as ex:
        svc.purchase(1, 2, None, Decimal("5.00"))
    assert ex.value.status_code == 409
    assert ex.value.detail["code"] == "PRICE_CHANGED"
    assert ex.value.detail["actual_price"] == "10.00"
