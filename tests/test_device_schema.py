"""§1(2026-09-27, 앱 요청) — 기기 등록 platform 에 "ios_fcm" 허용.

숙제·복습 알림(일반 FCM)용 iOS 토큰. 착신(예약 통화)은 여전히 ios_voip 만 쓴다 —
그 배제는 tests/test_push_dispatch.py 에서 dispatch 경로로 못박는다. 여기는 등록
자체(스키마 검증)만 확인한다.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from domains.push.schemas.device import DeviceRegisterIn


@pytest.mark.parametrize("platform", ["android_fcm", "ios_voip", "ios_fcm"])
def test_device_register_accepts_all_three_platforms(platform):
    data = DeviceRegisterIn(platform=platform, token="tok-1")
    assert data.platform == platform


def test_device_register_rejects_unknown_platform():
    with pytest.raises(ValidationError):
        DeviceRegisterIn(platform="ios_push_bogus", token="tok-1")


def test_device_register_default_platform_is_android_fcm():
    """회귀 — 기본값(플랫폼 생략 시)은 그대로 android_fcm 이다."""
    data = DeviceRegisterIn(token="tok-1")
    assert data.platform == "android_fcm"
