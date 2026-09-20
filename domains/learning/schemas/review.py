"""review 관련 DTO."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class ReviewCreate(BaseModel):
    voice_url: Optional[str] = None  # 사용자 녹음 저장 위치(채점 대상)
    apply_score: bool = True  # False = 문장 공식점수(Evaluation) 미갱신(이력·채점만); 기본 True=하위호환


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


class PhonemeMissOut(BaseModel):
    """틀린 자모 1건 — 조음 도해가 「어느 소리를 보여줄지」의 근거.

    char_index 는 **char_scores 와 같은 기준**(공백 제외 0-기준)이다. 앱이 이 값으로
    두 배열을 맞춘다 — 어긋나면 엉뚱한 글자에 도해가 붙는다.
    actual(실제로 낸 소리)은 아직 안 싣는다. 앱은 없어도 목표 도해 한 컷으로 동작한다.
    """

    char_index: int    # char_scores 의 인덱스(공백 제외 0-기준)
    expected: str      # 목표 자모(예: "ㄹ")


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
    # 채점 엔진이 자모를 못 주면 빈 목록 — 앱은 종전대로 동작한다(계약이 이미 열려 있음).
    phoneme_misses: list[PhonemeMissOut] = []
