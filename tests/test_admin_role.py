"""관리자 롤 회귀.

- /__dev/* 운영 도구는 ENV 게이트만으로 가려져 있었는데 실서비스조차 ENV="test" 라
  사실상 로그인한 아무 회원에게나 열려 있었다 → member.role == "admin" 으로 막는다.

(구매 가격 경합 409 시험은 POST /characters/{id}/purchase 삭제(2026-09-29)와 함께 지웠다.)
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from core.deps import ADMIN_ROLE, get_current_admin


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
