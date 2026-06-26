"""review 관련 DTO."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class ReviewCreate(BaseModel):
    voice_url: Optional[str] = None  # 사용자 녹음 저장 위치(채점 대상)


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    review_id: int
    sentence_id: int
    voice_url: Optional[str]
    created_at: datetime


# ── 발음 채점 피드백(페이지) ──
class CharScoreOut(BaseModel):
    char: str          # 글자
    score: int         # 0~100
    grade: str         # 상/중/하


class PronScoreOut(BaseModel):
    total_score: int
    pronunciation: int
    fluency: int
    rhythm: int


class ReviewFeedback(BaseModel):
    """복습 채점 결과 화면 — 한국어 문장 + 글자별 상/중/하 + 평가 점수 + 모국어 문장."""

    review_id: int
    sentence_id: int
    korean_sentence: Optional[str]
    native_sentence: Optional[str]
    voice_url: Optional[str]
    evaluation: PronScoreOut
    char_scores: list[CharScoreOut]
