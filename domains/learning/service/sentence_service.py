"""SentenceService — 북마크 토글 + 북마크 목록. 소유는 call 경유 검증."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from core import storage, tts
from core.config import settings
from domains.learning.models.sentence import Sentence
from domains.learning.repository.sentence_repository import SentenceRepository
from domains.learning.schemas.call import EvaluationOut, SentenceOut
from domains.learning.schemas.sentence import SentenceTtsOut

logger = logging.getLogger(__name__)


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

    async def synthesize_tts(
        self, member_id: int, sentence_id: int, client: Any | None
    ) -> SentenceTtsOut:
        """문장 단건 온디맨드 TTS — 이미 있으면 재사용, 없으면 합성→저장→URL 반환.

        idempotent: voice_url 이 이미 있으면 재합성 없이 그대로 반환. 핸들러(async)에서
        호출한다 — 합성(await)은 DB 세션 밖에서 하고, 저장만 동기 세션으로 처리한다.

        에러:
            404 — 없거나 타인 소유 문장(_get_owned).
            422 — korean_sentence 가 비어있음.
            503 — genai 클라이언트 None / 합성 실패 / 업로드 실패(오디오 생성 불가).
        """
        sentence = self._get_owned(member_id, sentence_id)

        # 1) idempotent: 이미 음성이 있으면 재합성 없이 그대로.
        if sentence.voice_url:
            return SentenceTtsOut(sentence_id=sentence_id, voice_url=sentence.voice_url)

        # 2) 합성 대상 텍스트 검증.
        korean = (sentence.korean_sentence or "").strip()
        if not korean:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "합성할 한국어 문장이 없습니다.",
            )

        call_id = sentence.call_id

        # 3) genai 미구성이면 합성 불가 → 503.
        if client is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "오디오를 생성할 수 없습니다(TTS 비활성).",
            )

        # 4) Vertex Gemini-TTS 합성(await) — DB 세션 밖에서.
        synthesized = await tts.synthesize_korean(korean, client)
        if not synthesized:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "오디오를 생성할 수 없습니다.",
            )
        audio, content_type = synthesized

        # 5) public 버킷 업로드(기존 분석 파이프라인과 동일한 key 규칙).
        ext = "mp3" if content_type == "audio/mpeg" else "wav"
        path = f"tts/{call_id}/{sentence_id}.{ext}"
        key = storage.upload(settings.SUPABASE_BUCKET_SAMPLES, path, audio, content_type)
        url = storage.public_url(settings.SUPABASE_BUCKET_SAMPLES, key) if key else None
        if not url:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "오디오 저장에 실패했습니다.",
            )

        # 6) voice_url 저장(동기 세션). 재조회로 stale 회피 후 커밋.
        fresh = self.db.get(Sentence, sentence_id)
        if fresh is not None:
            fresh.voice_url = url
            self.db.commit()
        logger.info("on-demand TTS: sentence_id=%s 합성 완료 → %s", sentence_id, path)
        return SentenceTtsOut(sentence_id=sentence_id, voice_url=url)

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
