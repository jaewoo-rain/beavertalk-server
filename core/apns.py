"""APNs VoIP 발송 어댑터(token-based JWT ES256, HTTP/2). 키 없으면 graceful 비활성.

core/fcm.py 규율 미러: DB/도메인을 모르는 순수 어댑터. 키 미설정·의존성 미설치·
임의 예외를 모두 흡수하고 발송만 비활성화한다(등록/삭제·android 발송 무영향).
개인키는 settings.APNS_PRIVATE_KEY(.p8 내용, Secret Manager) 우선, 없으면
APNS_PRIVATE_KEY_FILE(.p8 경로, 로컬)에서 읽는다 — FCM_SERVICE_ACCOUNT_JSON/_FILE 규율.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from core.config import settings

logger = logging.getLogger(__name__)

# APNs provider JWT 는 재사용 권장(20분~1시간). 매 발송 재발급하면 429
# (TooManyProviderTokenUpdates) 위험 → 모듈 캐시(TTL). 예외는 전부 흡수.
_JWT_TTL_S = 50 * 60
_jwt_cache: dict = {"token": None, "exp": 0.0}
_lock = threading.Lock()


@dataclass
class ApnsSendResult:
    sent: int = 0
    dead_tokens: list = field(default_factory=list)


def _private_key() -> str | None:
    """개인키(.p8 PEM) — 내용(env) 우선, 없으면 파일 경로에서 읽기(로컬 폴백)."""
    if settings.APNS_PRIVATE_KEY:
        return settings.APNS_PRIVATE_KEY
    path = settings.APNS_PRIVATE_KEY_FILE
    if path:
        try:
            import pathlib

            p = pathlib.Path(path)
            if p.is_file():
                return p.read_text()
            logger.warning("APNs .p8 파일 없음(%s) → iOS 발송 비활성", path)
        except Exception as exc:  # noqa: BLE001 - 파일 읽기 실패 graceful
            logger.warning("APNs .p8 파일 읽기 실패 → iOS 발송 비활성: %s", exc)
    return None


def _jwt() -> str | None:
    """APNs provider JWT(ES256) — 캐시 재사용(TTL). 키/의존성 부재 시 None(발송 비활성)."""
    if not (settings.APNS_KEY_ID and settings.APNS_TEAM_ID):
        return None
    key = _private_key()
    if not key:
        return None
    now = time.time()
    with _lock:
        if _jwt_cache["token"] and _jwt_cache["exp"] > now:
            return _jwt_cache["token"]
        try:
            import jwt  # PyJWT (pyjwt[crypto] — ES256 은 cryptography 필요)

            token = jwt.encode(
                {"iss": settings.APNS_TEAM_ID, "iat": int(now)},
                key,
                algorithm="ES256",
                headers={"kid": settings.APNS_KEY_ID},
            )
        except Exception as exc:  # noqa: BLE001 - 미설치/키형식 오류 graceful
            logger.warning("APNs JWT 생성 실패 → iOS 발송 비활성: %s", exc)
            return None
        _jwt_cache["token"] = token
        _jwt_cache["exp"] = now + _JWT_TTL_S
        return token


def warmup() -> None:
    """lifespan 워밍업(콜드스타트에 첫 링 지연 방지). 실패해도 무시."""
    _jwt()


def send_incoming_call_voip(
    *, tokens, call_id: str, character_id: int, name, image_url=None
) -> ApnsSendResult:
    """착신(예약전화) VoIP 푸시. 앱 계약: {id,nameCaller,handle,isVideo,extra:{characterId}}.

    image_url 은 fcm 시그니처 호환용으로만 받고 페이로드엔 넣지 않는다(앱 계약 외).
    폐기 토큰(BadDeviceToken/Unregistered)은 dead_tokens 로 보고 → 호출부가 무효화.
    키/httpx 부재·발송 예외는 모두 흡수(android/기존 무영향).
    """
    result = ApnsSendResult()
    jt = _jwt()
    if jt is None or not tokens:
        if jt is None:
            logger.warning("APNs 미설정/키오류 → iOS 발송 비활성")
        return result
    host = (
        "https://api.sandbox.push.apple.com"
        if settings.APNS_USE_SANDBOX
        else "https://api.push.apple.com"
    )
    headers = {
        "authorization": f"bearer {jt}",
        "apns-topic": f"{settings.APNS_BUNDLE_ID}.voip",
        "apns-push-type": "voip",
        "apns-priority": "10",
        "apns-expiration": "0",
    }
    payload = {
        "id": call_id,
        "nameCaller": name or "비버 튜터",
        "handle": "한국어 통화",
        "isVideo": False,
        "extra": {"characterId": character_id},
        "aps": {},
    }
    try:
        import httpx

        with httpx.Client(http2=True, timeout=15) as c:
            for tok in tokens:
                try:
                    r = c.post(f"{host}/3/device/{tok}", headers=headers, json=payload)
                except Exception as exc:  # noqa: BLE001 - 개별 토큰 예외 흡수(다음 토큰 계속)
                    logger.warning("APNs 발송 예외 %s…: %s", tok[:12], exc)
                    continue
                if r.status_code == 200:
                    result.sent += 1
                    continue
                reason = ""
                try:
                    reason = r.json().get("reason", "")
                except Exception:  # noqa: BLE001 - 응답 파싱 실패 무시
                    pass
                if reason in ("BadDeviceToken", "Unregistered"):
                    result.dead_tokens.append(tok)
                    logger.info("APNs 토큰 폐기 %s…: %s", tok[:12], reason)
                else:
                    logger.warning(
                        "APNs 발송 실패 %s…: %s %s", tok[:12], r.status_code, reason
                    )
    except Exception as exc:  # noqa: BLE001 - httpx 미설치/클라이언트 예외 graceful
        logger.warning("APNs 클라이언트 예외 → iOS 발송 비활성: %s", exc)
    return result
