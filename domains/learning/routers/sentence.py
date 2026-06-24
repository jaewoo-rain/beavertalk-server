"""sentence 라우터 — 북마크 + 복습. + 내 북마크 모음."""

from __future__ import annotations

from fastapi import APIRouter, status

from core.deps import CurrentMember, DbSession
from domains.learning.schemas.call import SentenceOut
from domains.learning.schemas.review import ReviewCreate, ReviewFeedback, ReviewOut
from domains.learning.schemas.sentence import SentenceBookmarkUpdate
from domains.learning.service.review_service import ReviewService
from domains.learning.service.sentence_service import SentenceService

router = APIRouter(tags=["sentences"])


@router.patch("/sentences/{sentence_id}/bookmark", response_model=SentenceOut)
def set_bookmark(
    sentence_id: int, data: SentenceBookmarkUpdate, member: CurrentMember, db: DbSession
) -> SentenceOut:
    return SentenceService(db).set_bookmark(member.member_id, sentence_id, data.is_bookmarked)


@router.get("/members/me/bookmarks", response_model=list[SentenceOut])
def my_bookmarks(member: CurrentMember, db: DbSession) -> list[SentenceOut]:
    return SentenceService(db).list_bookmarks(member.member_id)


@router.delete("/sentences/{sentence_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sentence(sentence_id: int, member: CurrentMember, db: DbSession) -> None:
    """문장 소프트 삭제(행 보존, 읽기에서 제외)."""
    SentenceService(db).soft_delete(member.member_id, sentence_id)


@router.post(
    "/sentences/{sentence_id}/reviews",
    response_model=ReviewFeedback,
    status_code=status.HTTP_201_CREATED,
)
def add_review(
    sentence_id: int, data: ReviewCreate, member: CurrentMember, db: DbSession
) -> ReviewFeedback:
    """녹음 제출 → 발음 채점 → 글자별 상/중/하 + 평가 점수 반환."""
    return ReviewService(db).add_review(member.member_id, sentence_id, data)


@router.get("/sentences/{sentence_id}/reviews", response_model=list[ReviewOut])
def list_reviews(sentence_id: int, member: CurrentMember, db: DbSession) -> list[ReviewOut]:
    return ReviewService(db).list_reviews(member.member_id, sentence_id)


@router.get("/reviews/{review_id}/feedback", response_model=ReviewFeedback)
def get_review_feedback(
    review_id: int, member: CurrentMember, db: DbSession
) -> ReviewFeedback:
    """복습 채점 결과 페이지 — 한국어/모국어 문장 + 글자별 상중하 + 평가 점수."""
    return ReviewService(db).get_feedback(member.member_id, review_id)
