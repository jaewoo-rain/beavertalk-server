"""Supabase Storage 업로드/URL 어댑터 (normalcall) — graceful.

통화 원본 음성·표현 TTS·연습 녹음을 Supabase Storage 에 올리고 object key 를
반환한다. SUPABASE_URL/SERVICE_KEY 미설정·supabase 미설치·임의 예외를 모두 흡수해
None 을 반환한다(speechsuper.py 규율) — 저장이 안 돼도 통화/분석은 죽지 않는다.

버킷 규약(플랜 §8):
    voice-samples(public)     : 캐릭터 프리뷰·표현 TTS   → public_url
    voice-recordings(private) : 통화 원본·연습 녹음        → signed_url

DB(voice_url 컬럼)에는 **object key(버킷 상대 경로)만** 저장하고, 재생 URL 은
public_url / signed_url 로 그때그때 조립한다.
"""

from __future__ import annotations

import logging
from typing import Any

from core.config import settings

logger = logging.getLogger(__name__)

_client: "Any | None" = None
_client_ready = False


def _get_client() -> "Any | None":
    """Supabase 클라이언트를 lazy 생성(없으면 None, 1회 경고). service_role 키 사용."""
    global _client, _client_ready
    if _client_ready:
        return _client

    _client_ready = True
    url, key = settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY
    if not url or not key:
        logger.warning("storage: SUPABASE_URL/SERVICE_KEY 미설정 → 업로드 비활성(voice_url=None).")
        _client = None
        return None
    try:
        from supabase import create_client

        _client = create_client(url, key)
        logger.info("storage: Supabase 클라이언트 초기화 완료.")
    except Exception as exc:  # noqa: BLE001 - 미설치/인증/임의 예외 graceful
        logger.warning("storage: Supabase 비활성(업로드 스킵) — %s", exc)
        _client = None
    return _client


def upload(bucket: str, path: str, data: bytes, content_type: str = "application/octet-stream") -> str | None:
    """data 를 bucket/path 로 업로드하고 object key(path)를 반환한다(graceful None).

    upsert=true 로 동일 경로 덮어쓰기 허용(재시도 안전). 실패 시 None.
    """
    if not data:
        return None
    client = _get_client()
    if client is None:
        return None
    try:
        client.storage.from_(bucket).upload(
            path=path,
            file=data,
            file_options={"content-type": content_type, "upsert": "true"},
        )
        logger.info("storage: 업로드 성공 %s/%s (%d bytes)", bucket, path, len(data))
        return path
    except Exception as exc:  # noqa: BLE001 - 업로드 실패 graceful
        logger.warning("storage: 업로드 실패(무시, None) %s/%s — %s", bucket, path, exc)
        return None


def public_url(bucket: str, path: str | None) -> str | None:
    """public 버킷 객체의 재생 URL. path 없거나 실패 시 None."""
    if not path:
        return None
    client = _get_client()
    if client is None:
        return None
    try:
        return client.storage.from_(bucket).get_public_url(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("storage: public_url 실패 %s/%s — %s", bucket, path, exc)
        return None


def signed_url(bucket: str, path: str | None, expires_in: int = 3600) -> str | None:
    """private 버킷 객체의 서명 재생 URL(기본 1시간). path 없거나 실패 시 None."""
    if not path:
        return None
    client = _get_client()
    if client is None:
        return None
    try:
        res = client.storage.from_(bucket).create_signed_url(path, expires_in)
        # supabase-py 버전에 따라 키가 signedURL / signed_url 로 다를 수 있어 둘 다 대응.
        if isinstance(res, dict):
            return res.get("signedURL") or res.get("signed_url") or res.get("signedUrl")
        return res
    except Exception as exc:  # noqa: BLE001
        logger.warning("storage: signed_url 실패 %s/%s — %s", bucket, path, exc)
        return None
