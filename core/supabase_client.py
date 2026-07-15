"""Supabase service_role 클라이언트 팩토리(공유).

인증 토큰 검증(core.supabase_auth)이 이 클라이언트를 재사용한다. 과거엔 core.storage 가
이 클라이언트를 소유했으나, 오디오 저장이 Supabase Storage → GCS 로 이전되며 분리했다.
인증 주체는 여전히 Supabase(GoTrue)이므로 클라이언트 자체는 남는다.

미설정/미설치/예외 → None (호출부가 graceful 처리 — 인증 불가 시 401).
"""

from __future__ import annotations

import logging
from typing import Any

from core.config import settings

logger = logging.getLogger(__name__)

_client: "Any | None" = None
_ready = False


def get_client() -> "Any | None":
    """Supabase 클라이언트를 lazy 생성(없으면 None, 1회 경고). service_role 키 사용."""
    global _client, _ready
    if _ready:
        return _client

    _ready = True
    url, key = settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY
    if not url or not key:
        logger.warning("supabase_client: SUPABASE_URL/SERVICE_KEY 미설정 → 비활성.")
        _client = None
        return None
    try:
        from supabase import create_client

        _client = create_client(url, key)
        logger.info("supabase_client: 초기화 완료.")
    except Exception as exc:  # noqa: BLE001 - 미설치/인증/임의 예외 graceful
        logger.warning("supabase_client: 비활성 — %s", exc)
        _client = None
    return _client
