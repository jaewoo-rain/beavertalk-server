"""공통 스키마 — 페이지네이션·표준 에러. 모든 도메인이 공유."""

from __future__ import annotations

from typing import Generic, Optional, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """오프셋 페이지네이션 응답 래퍼.

    사용 예: `Page[MemberRead]` → {items: [...], total, limit, offset, has_more}
    """

    items: list[T]
    total: Optional[int] = None
    limit: int
    offset: int
    has_more: bool


class ErrorDetail(BaseModel):
    """표준 에러 바디. {"detail": {"code": ..., "message": ...}}"""

    code: str
    message: str


class ErrorResponse(BaseModel):
    detail: ErrorDetail
