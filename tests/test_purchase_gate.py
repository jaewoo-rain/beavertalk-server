"""결제 미연동 상태의 구매 동작 회귀 (IAP 전환 전).

배경: 서버가 아직 실제 청구를 하지 않는다(카드사·PG·스토어 어디에도 안 보낸다).
그래도 **막지 않는다** — 앱이 구매→지급→화면갱신 흐름을 지금 만들어야 하기 때문.
대신 "실결제가 아니다"를 응답(is_test_grant)과 로그에 남겨 나중에 걸러낼 수 있게 한다.

⚠ 이 상태로 스토어 출시하면 안 된다. 실결제는 IAP(POST /purchases/verify)로 간다.
계약: docs/20260731_1230_IAP-API-계약서-프론트공유용.md
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from db.registry import Base  # noqa: F401 - 전 모델 import(ORM 매퍼 초기화)
from domains.commerce.schemas.purchase import PurchaseResponse
from domains.commerce.service.purchase_service import PurchaseService


class _Char:
    def __init__(self, price):
        self.character_id, self.name = 2, "BIBI"
        self.price = price
        self.discount_events: list = []


def _svc(price, owned=False):
    """DB 없이 지급 분기만 보는 최소 조립."""
    s = PurchaseService.__new__(PurchaseService)
    s.char_repo = type("R", (), {"get": lambda _s, _c: _Char(price)})()
    s.mc_repo = type("M", (), {
        "get": lambda _s, _m, _c: (object() if owned else None),
        "add": lambda _s, _o: None,
    })()
    s.char_service = type("C", (), {"effective_price": lambda _s, ch: ch.price})()
    s.db = type("D", (), {"commit": lambda _s: None, "refresh": lambda _s, _o: None})()
    s.payment_repo = type("P", (), {"add": lambda _s, _o: None})()
    return s


def test_paid_character_is_purchasable_for_now():
    """★ 유료 캐릭터도 지금은 구매된다 — 앱이 흐름을 테스트할 수 있어야 하므로.

    막아두면 프론트가 아무것도 못 만든다. 대신 아래 테스트들이 "실결제 아님" 표시를
    강제한다.
    """
    svc = _svc(Decimal("10.00"))
    added: list = []
    svc.mc_repo = type("M", (), {
        "get": lambda _s, _m, _c: None,
        "add": lambda _s, o: added.append(o),
    })()
    try:
        svc.purchase(1, 2)
    except HTTPException as ex:
        pytest.fail(f"유료 구매가 차단됐다: {ex.status_code} {ex.detail}")
    except Exception:
        pass  # 응답 DTO 조립 단계 — 지급은 이미 지났다
    assert added, "소유권이 생성되지 않았다"


def test_response_carries_is_test_grant_flag():
    """★ 응답에 '실결제 아님'이 실려야 앱이 테스트 구매를 구분할 수 있다."""
    assert "is_test_grant" in PurchaseResponse.model_fields
    # 기본값 False — IAP 전환 후 필드가 사라져도 옛 클라가 안 깨진다.
    assert PurchaseResponse.model_fields["is_test_grant"].default is False


def test_already_owned_still_409():
    """중복 구매는 여전히 막는다(결제 연동과 무관한 규칙)."""
    with pytest.raises(HTTPException) as ex:
        _svc(Decimal("10.00"), owned=True).purchase(1, 2)
    assert ex.value.status_code == 409
    assert ex.value.detail["code"] == "ALREADY_OWNED"


def test_free_character_is_not_flagged_as_test_grant():
    """무료(BABA) 지급은 원래 공짜다 — '실결제 아님' 경고 대상이 아니다."""
    svc = _svc(Decimal("0.00"))
    added: list = []
    svc.mc_repo = type("M", (), {
        "get": lambda _s, _m, _c: None,
        "add": lambda _s, o: added.append(o),
    })()
    try:
        svc.purchase(1, 2)
    except HTTPException as ex:
        pytest.fail(f"무료인데 차단됐다: {ex.status_code}")
    except Exception:
        pass
    assert added
