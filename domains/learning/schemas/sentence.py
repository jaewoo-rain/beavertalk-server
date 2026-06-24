"""sentence 관련 DTO(북마크 토글)."""

from __future__ import annotations

from pydantic import BaseModel


class SentenceBookmarkUpdate(BaseModel):
    is_bookmarked: bool
