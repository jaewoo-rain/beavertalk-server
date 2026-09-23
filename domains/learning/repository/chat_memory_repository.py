"""ChatMemoryRepository — 자유대화 기억 저장소 조회/추가(C6). 순수 DB 접근, commit 안 함."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from domains.learning.models.chat_memory import ChatMemory


class ChatMemoryRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, member_id: int, language: str) -> ChatMemory | None:
        stmt = select(ChatMemory).where(
            ChatMemory.member_id == member_id, ChatMemory.language == language,
        )
        return self.db.scalars(stmt).first()

    def add(self, row: ChatMemory) -> ChatMemory:
        self.db.add(row)
        return row
