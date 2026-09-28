"""device 관련 DTO — 푸시 토큰 등록/응답."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


# ── 요청 ──
class DeviceRegisterIn(BaseModel):
    # ⭐ §1(2026-09-27, 앱 요청) — "ios_fcm" 추가: 숙제·복습 알림(일반 FCM)용 iOS 토큰.
    #   ⛔ 착신(예약 통화) 디스패치는 여전히 "ios_voip" 만 쓴다 — 일반 알림 토큰으로
    #   CallKit 착신을 못 띄운다(VoIP push 전용 채널이 따로 있다). dispatch_service.py
    #   의 _ring() 이 android_fcm/ios_voip 만 필터링해서 자동으로 제외된다(시험으로 못박음).
    platform: Literal["android_fcm", "ios_voip", "ios_fcm"] = "android_fcm"
    token: str
    app_version: Optional[str] = None


# ── 응답 ──
class DeviceOut(BaseModel):
    device_token_id: int
    platform: str
