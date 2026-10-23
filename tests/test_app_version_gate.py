"""PM-DEC-1035 — 대본 읽기·통화 반응은 소켓 app_version ≥ 1.0.1 일 때만(스토어 71 은 그 화면이 없다)."""
from __future__ import annotations

import pytest

from core.client_info import app_at_least


@pytest.mark.parametrize("v,ok", [
    ("1.0.1+93", True), ("1.0.1", True), ("1.0.2+1", True), ("1.1.0+5", True), ("2.0.0", True),
    ("1.0.0+71", False), ("1.0.0+92", False), ("0.9.9", False),
    (None, False), ("", False), ("garbage", False), ("1", False),
])
def test_app_at_least(v, ok):
    assert app_at_least(v, "1.0.1") is ok


def test_build_number_alone_does_not_pass():
    assert app_at_least("1.0.0+999", "1.0.1") is False   # 빌드 번호가 커도 버전 이름이 1.0.0 이면 옛 앱
