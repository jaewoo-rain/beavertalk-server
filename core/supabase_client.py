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
    """Supabase 클라이언트를 lazy 생성. **성공했을 때만 캐시**한다.

    ⚠️ 과거 버그: 생성 실패(콜드스타트 일시 오류·설정 지연 등)에도 None 을 영구 캐싱해
    그 인스턴스는 이후 모든 인증이 401 로 죽었다(설정·키가 멀쩡해도). 이제 **성공 시에만
    _ready 로 캐시**하고, 실패는 캐시하지 않아 다음 호출에서 재시도한다.
    """
    global _client, _ready
    if _ready:
        return _client

    url, key = settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY
    if not url or not key:
        logger.warning("supabase_client: SUPABASE_URL/SERVICE_KEY 미설정 → 비활성(다음 호출 재시도).")
        return None  # 캐시 안 함 — 설정이 들어오면 재시도
    try:
        from supabase import create_client

        client = create_client(url, key)
        _client = client
        _ready = True  # 성공했을 때만 캐시(영구)
        logger.info("supabase_client: 초기화 완료.")
        return client
    except Exception as exc:  # noqa: BLE001 - 미설치/인증/임의 예외 graceful
        logger.warning("supabase_client: 생성 실패(다음 호출 재시도) — %s", exc)
        return None  # 실패는 캐시 안 함 — 다음 호출에서 재시도
