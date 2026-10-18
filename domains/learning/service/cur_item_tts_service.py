"""차시 표현 항목 TTS(PM-DEC-796) — 통화 i 시트 「이번 차시 표현」 행의 소리.

POST /api/v1/cur/items/{item_id}/tts → 서명한 재생 URL.

- 같은 항목은 **한 번만 합성**한다. 저장 위치는 항목 id + 표면형 해시로 정해진 GCS key 다(DB 칸 없음).
  표면형이 바뀌면 key 가 바뀌어 새로 합성된다(옛 소리를 계속 쓰지 않는다).
- 같은 인스턴스 안의 동시 요청은 항목별 잠금으로 한 번만 합성한다. 다른 인스턴스와 겹치면 같은 key 로 덮어쓰므로
  결과는 같고 비용만 한 번 더 든다(행 잠금을 쓰려면 DB 칸이 필요해 이번 범위 밖).
- 로그인 회원만(라우터 CurrentMember) · 존재하고 은퇴하지 않은 항목만(없으면 404).
- 합성·저장·서명 실패는 503 — 앱은 버튼을 숨기지 않고 비활성으로 둔다.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from core import storage, tts
from core.config import settings
from core.prompts.locked.freetalk import fill_slots
from domains.learning.models.curriculum import CurItem

logger = logging.getLogger(__name__)

#: 항목별 합성 잠금(같은 인스턴스 동시 요청 1회 합성).
_locks: dict[str, asyncio.Lock] = {}
#: 이 인스턴스가 이미 있다고 확인한 key — 목록 조회 캐시(storage.existing_keys TTL)보다 앞선다.
_known: set[str] = set()


def _bucket() -> str:
    return settings.SUPABASE_BUCKET_SAMPLES


def tts_text(item: CurItem) -> str:
    """읽을 문장 — 표면형(빈칸 «◯◯» 은 화면과 같은 규칙으로 채운다)."""
    return fill_slots((item.surface or "").strip())


def object_path(item: CurItem) -> str:
    text = tts_text(item)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"tts/cur_items/{item.language}/{int(item.item_id)}_{digest}.mp3"


def _playback(key: str) -> str:
    url = storage.playback_url(_bucket(), key, settings.GCS_SIGNED_URL_TTS_TTL)
    if not url:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "오디오를 재생할 수 없습니다.")
    return url


def _exists(key: str) -> bool:
    if key in _known:
        return True
    keys = storage.existing_keys(_bucket(), key)
    if keys and key in keys:
        _known.add(key)
        return True
    return False


async def item_tts(db: Session, item_id: int) -> dict:
    item = db.get(CurItem, item_id)
    if item is None or item.retired_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "항목을 찾을 수 없습니다.")
    text = tts_text(item)
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "읽을 문장이 없습니다.")
    key = object_path(item)
    if _exists(key):
        return {"item_id": int(item.item_id), "voice_url": _playback(key)}
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        if not _exists(key):          # 잠금을 기다리는 동안 앞 요청이 만들었으면 그대로 쓴다
            synthesized = await tts.synthesize(text, item.language or "ko")
            if not synthesized:
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "오디오를 생성할 수 없습니다.")
            audio, content_type = synthesized
            stored = storage.upload(_bucket(), key, audio, content_type)
            if not stored:
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "오디오 저장에 실패했습니다.")
            _known.add(key)
            logger.info("cur item TTS: item_id=%s 합성 %d자 → %s", item.item_id, len(text), key)
    return {"item_id": int(item.item_id), "voice_url": _playback(key)}
