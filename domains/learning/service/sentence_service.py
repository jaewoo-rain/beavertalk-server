"""SentenceService — 북마크 토글 + 북마크 목록. 소유는 call 경유 검증."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from domains.learning.models.sentence import Sentence
from domains.learning.repository.sentence_repository import SentenceRepository
from domains.learning.schemas.call import EvaluationOut, SentenceOut


class SentenceService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = SentenceRepository(db)

    def set_bookmark(self, member_id: int, sentence_id: int, value: bool) -> SentenceOut:
        sentence = self._get_owned(member_id, sentence_id)
        sentence.is_bookmarked = value
        self.db.commit()
        self.db.refresh(sentence)
        return self._to_out(sentence)

    def list_bookmarks(self, member_id: int) -> list[SentenceOut]:
        return [self._to_out(s) for s in self.repo.list_bookmarked(member_id)]

    def soft_delete(self, member_id: int, sentence_id: int) -> None:
        """문장 소프트 삭제 — 행은 남기고 deleted_at 만 기록(읽기에서 제외됨)."""
        sentence = self._get_owned(member_id, sentence_id)
        sentence.deleted_at = datetime.now(timezone.utc)
        self.db.commit()

    # ── 내부 ──
    def _get_owned(self, member_id: int, sentence_id: int) -> Sentence:
        sentence = self.repo.get(sentence_id)
        # 발화의 소유는 그 발화가 속한 통화(call)의 회원으로 판단.
        # 이미 소프트 삭제된 발화는 없는 것으로 취급(404).
        if (
            sentence is None
            or sentence.deleted_at is not None
            or sentence.call.member_id != member_id
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "발화를 찾을 수 없습니다.")
        return sentence

    def _to_out(self, s: Sentence) -> SentenceOut:
        return SentenceOut(
            sentence_id=s.sentence_id,
            korean_sentence=s.korean_sentence,
            native_sentence=s.native_sentence,
            locale=s.locale,
            voice_url=s.voice_url,
            is_bookmarked=s.is_bookmarked,
            evaluation=EvaluationOut.model_validate(s.evaluation) if s.evaluation else None,
        )
