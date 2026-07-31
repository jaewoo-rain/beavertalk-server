"""FCM data-only 발송 어댑터(firebase-admin). 키 없으면 graceful 비활성(앱은 정상).

storage.py/speechsuper.py 규율: DB/도메인을 모르는 순수 어댑터. 서비스계정
미설정·firebase-admin 미설치·임의 예외를 모두 흡수하고, 발송만 비활성화한다.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import timedelta

from core.config import settings
from core.push_defaults import DEFAULT_CALLER_NAME

logger = logging.getLogger(__name__)
_app = None
_lock = threading.Lock()


@dataclass
class FcmSendResult:
    sent: int = 0
    dead_tokens: list = field(default_factory=list)


def _ensure_app():
    """firebase_admin 앱을 lazy 초기화(1회). 키 부재/초기화 실패 시 None(발송 비활성)."""
    global _app
    if _app is not None:
        return _app
    with _lock:
        if _app is not None:
            return _app
        try:
            import firebase_admin
            from firebase_admin import credentials

            if settings.FCM_SERVICE_ACCOUNT_JSON:
                cred = credentials.Certificate(
                    json.loads(settings.FCM_SERVICE_ACCOUNT_JSON)
                )
            elif settings.FCM_SERVICE_ACCOUNT_FILE:
                import pathlib

                if not pathlib.Path(settings.FCM_SERVICE_ACCOUNT_FILE).is_file():
                    logger.warning("FCM 서비스계정 파일 없음 → 발송 비활성")
                    return None
                cred = credentials.Certificate(settings.FCM_SERVICE_ACCOUNT_FILE)
            else:
                logger.warning("FCM 서비스계정 미설정 → 발송 비활성")
                return None
            _app = firebase_admin.initialize_app(
                cred, {"projectId": settings.FIREBASE_PROJECT_ID}
            )
            return _app
        except Exception as exc:  # noqa: BLE001 - 미설치/인증/임의 예외 graceful
            logger.warning("firebase-admin 초기화 실패 → 발송 비활성: %s", exc)
            return None


def warmup() -> None:
    """lifespan 워밍업(콜드스타트에 첫 링 지연 방지). 실패해도 무시."""
    _ensure_app()


def send_incoming_call(
    *, tokens, call_id: str, character_id: int, name, image_url
) -> FcmSendResult:
    """착신(예약전화) data-only 멀티캐스트 발송. 폐기 토큰은 dead_tokens 로 보고."""
    app = _ensure_app()
    if app is None or not tokens:
        return FcmSendResult()
    from firebase_admin import messaging

    msg = messaging.MulticastMessage(
        tokens=list(tokens),
        data={
            "call_id": call_id,
            "character_id": str(character_id),
            "name": name or DEFAULT_CALLER_NAME,
            "image_url": image_url or "",
        },
        android=messaging.AndroidConfig(
            priority="high", ttl=timedelta(seconds=60)
        ),
    )
    try:
        resp = messaging.send_each_for_multicast(msg, app=app)
    except Exception as exc:  # noqa: BLE001 - 발송 실패 graceful
        logger.warning("FCM 멀티캐스트 발송 실패: %s", exc)
        return FcmSendResult()
    result = FcmSendResult()
    for tok, r in zip(tokens, resp.responses):
        if r.success:
            result.sent += 1
        else:
            exc = r.exception
            # 폐기 대상(영구 실패) 토큰만 무효화. 일시 실패는 다음 링에 재시도.
            if isinstance(
                exc, (messaging.UnregisteredError, messaging.SenderIdMismatchError)
            ):
                result.dead_tokens.append(tok)
                logger.info("FCM 토큰 폐기: %s…", tok[:12])
            else:
                logger.warning("FCM 발송 실패(일시) %s…: %s", tok[:12], exc)
    return result
