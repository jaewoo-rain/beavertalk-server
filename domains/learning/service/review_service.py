"""ReviewService — 복습 추가(발음 채점) + 피드백 조회. 소유는 call 경유 검증."""

from __future__ import annotations

import contextlib
import os
import tempfile
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from core import audio as audio_mod
from core import storage
from core.config import settings
from core.speechsuper import assess_pronunciation
from domains.learning.models.evaluation import Evaluation
from domains.learning.models.review import Review
from domains.learning.models.sentence import Sentence
from domains.learning.repository.review_repository import ReviewRepository
from domains.learning.repository.sentence_repository import SentenceRepository
from domains.learning.schemas.review import ReviewCreate, ReviewFeedback, ReviewOut


class ReviewService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = ReviewRepository(db)
        self.sentence_repo = SentenceRepository(db)

    def add_review(
        self,
        member_id: int,
        sentence_id: int,
        data: ReviewCreate,
        audio_override: str | None = None,
    ) -> ReviewFeedback:
        """녹음 제출 → 발음 채점(SpeechSuper) → 저장 → 피드백 반환.

        audio_override 가 주어지면 채점은 그 경로/URL 로 하고(스토리지 미사용 폴백 등),
        DB 에는 data.voice_url(보통 object key, 폴백이면 None)을 저장한다.
        """
        sentence = self._get_owned_sentence(member_id, sentence_id)
        # voice_url 이 Storage object key 면 채점용으로 signed URL 을 만들어 SpeechSuper 가
        # 가져오게 하고, DB 에는 key 를 그대로 저장한다(스토리지 규약: key 보관 + URL 즉석조립).
        audio_ref = audio_override or self._audio_for_scoring(data.voice_url)
        feedback = assess_pronunciation(sentence.korean_sentence, audio_ref)
        review = Review(
            sentence_id=sentence_id,
            voice_url=data.voice_url,  # object key(또는 폴백 경로)
            feedback=feedback,
        )
        self.repo.add(review)
        # 발화의 공식 평가(Evaluation 1:1)도 '마지막 시도' 점수로 덮어쓴다.
        # → Sentence.evaluation / 통화 평균(CallResult.average)에 반영됨.
        self._apply_evaluation(sentence, feedback)
        self.db.commit()
        self.db.refresh(review)
        return self._to_feedback(review, sentence)

    def add_review_from_audio(
        self,
        member_id: int,
        sentence_id: int,
        raw: bytes,
        content_type: str | None = None,
    ) -> ReviewFeedback:
        """업로드된 녹음 바이트로 복습 채점 (멀티파트 엔드포인트 공용 로직).

        저장은 MP3(어디서든 재생; ffmpeg 없으면 원본 폴백), 채점은 무손실 원본(임시파일).
        클라이언트는 보통 WAV(pcm16 16k mono)를 보낸다. 스토리지 비활성이면 key=None(채점만).
        """
        # 저장용: MP3 인코딩(ffmpeg 자동 포맷감지). 실패하면 원본 그대로.
        mp3 = audio_mod.wav_to_mp3(raw)
        payload, ext, ctype = (
            (mp3, "mp3", "audio/mpeg") if mp3 else (raw, "wav", "audio/wav")
        )
        key = f"reviews/{member_id}/{sentence_id}/{uuid.uuid4().hex}.{ext}"
        stored = storage.upload(settings.SUPABASE_BUCKET_RECORDINGS, key, payload, ctype)
        # 채점은 항상 무손실 원본으로(임시 .wav). DB 에는 stored(또는 None) 저장.
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
            f.write(raw)
            tmp_path = f.name
        try:
            return self.add_review(
                member_id,
                sentence_id,
                ReviewCreate(voice_url=stored or None),
                audio_override=tmp_path,
            )
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

    @staticmethod
    def _audio_for_scoring(voice_url: str | None) -> str | None:
        """저장된 object key 면 signed URL 로 변환(채점 fetch용). 아니면 원본(URL/로컬 경로)."""
        if not voice_url:
            return None
        return (
            storage.signed_url(settings.SUPABASE_BUCKET_RECORDINGS, voice_url)
            or voice_url
        )

    def _apply_evaluation(self, sentence: Sentence, feedback: dict) -> None:
        """채점 결과의 평가 점수를 발화의 Evaluation 행에 반영(없으면 생성)."""
        score = (feedback or {}).get("evaluation") or {}
        ev = sentence.evaluation
        if ev is None:
            ev = Evaluation(sentence_id=sentence.sentence_id)
            self.db.add(ev)
            sentence.evaluation = ev
        ev.total_score = score.get("total_score")
        ev.pronunciation = score.get("pronunciation")
        ev.fluency = score.get("fluency")
        ev.rhythm = score.get("rhythm")

    def get_feedback(self, member_id: int, review_id: int) -> ReviewFeedback:
        review = self.repo.get(review_id)
        if review is None or review.sentence.call.member_id != member_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "복습을 찾을 수 없습니다.")
        if review.feedback is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "채점 결과가 없습니다.")
        return self._to_feedback(review, review.sentence)

    def list_reviews(self, member_id: int, sentence_id: int) -> list[ReviewOut]:
        self._get_owned_sentence(member_id, sentence_id)
        return [ReviewOut.model_validate(r) for r in self.repo.list_by_sentence(sentence_id)]

    # ── 내부 ──
    def _get_owned_sentence(self, member_id: int, sentence_id: int) -> Sentence:
        sentence = self.sentence_repo.get(sentence_id)
        if (
            sentence is None
            or sentence.deleted_at is not None
            or sentence.call.member_id != member_id
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "발화를 찾을 수 없습니다.")
        return sentence

    def _to_feedback(self, review: Review, sentence: Sentence) -> ReviewFeedback:
        # 외부 채점(SpeechSuper) 응답 형태가 바뀌어도 KeyError 안 나게 방어 접근
        fb = review.feedback or {}
        evaluation = fb.get("evaluation") or {
            "total_score": 0, "pronunciation": 0, "fluency": 0, "rhythm": 0,
        }
        return ReviewFeedback(
            review_id=review.review_id,
            sentence_id=review.sentence_id,
            korean_sentence=sentence.korean_sentence,
            native_sentence=sentence.native_sentence,
            voice_url=review.voice_url,
            evaluation=evaluation,
            char_scores=fb.get("char_scores", []),
        )
