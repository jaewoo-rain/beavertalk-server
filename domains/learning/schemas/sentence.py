"""sentence 관련 DTO(북마크 토글)."""

from __future__ import annotations

from pydantic import BaseModel


class SentenceBookmarkUpdate(BaseModel):
    is_bookmarked: bool


class SentenceTtsOut(BaseModel):
    """문장 단건 온디맨드 TTS 응답 — 합성/재사용된 재생 URL."""

    sentence_id: int
    voice_url: str
