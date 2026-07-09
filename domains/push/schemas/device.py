"""device 관련 DTO — 푸시 토큰 등록/응답."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


# ── 요청 ──
class DeviceRegisterIn(BaseModel):
    platform: Literal["android_fcm", "ios_voip"] = "android_fcm"
    token: str
    app_version: Optional[str] = None


# ── 응답 ──
class DeviceOut(BaseModel):
    device_token_id: int
    platform: str
