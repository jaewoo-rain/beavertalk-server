"""ReviewService — 복습 추가(발음 채점) + 피드백 조회. 소유는 call 경유 검증."""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

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

    def add_review(self, member_id: int, sentence_id: int, data: ReviewCreate) -> ReviewFeedback:
        """녹음 제출 → 발음 채점(SpeechSuper 스텁) → 저장 → 피드백 반환."""
        sentence = self._get_owned_sentence(member_id, sentence_id)
        feedback = assess_pronunciation(sentence.korean_sentence, data.voice_url)
        review = Review(
            sentence_id=sentence_id,
            record_time=data.record_time,
            voice_url=data.voice_url,
            feedback=feedback,
        )
        self.repo.add(review)
        # 발화의 공식 평가(Evaluation 1:1)도 '마지막 시도' 점수로 덮어쓴다.
        # → Sentence.evaluation / 통화 평균(CallResult.average)에 반영됨.
        self._apply_evaluation(sentence, feedback)
        self.db.commit()
        self.db.refresh(review)
        return self._to_feedback(review, sentence)

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
