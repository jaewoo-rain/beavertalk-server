"""발음 상세·이력 응답 스키마 (pydantic v2) — learning 도메인.

T8 `GET /calls/{call_id}/pronunciation` : 통화별 발음 상세(문장별 점수 + 소리별 집계 + 코칭 한마디).
T9 `GET /calls/pronunciation-history`    : 최근 5통화 발음 추이(날짜·문장수·평균 점수).

ORM→DTO 변환은 service 경계에서 수행하고, 라우터는 이 스키마로 검증·직렬화만 한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class PronSentenceScore(BaseModel):
    """문장 1건의 발음 점수(공식점수=Evaluation 직독). 미복습이면 점수 전부 None."""

    sentence_id: int
    korean_sentence: Optional[str] = None
    total_score: Optional[int] = None
    pronunciation: Optional[int] = None
    fluency: Optional[int] = None
    rhythm: Optional[int] = None


class SoundAggregate(BaseModel):
    """자모(alpha) 1개의 소리별 집계 — 문장별 마지막 counted 복습의 phonemes 만 산입."""

    alpha: str
    attempts: int
    passes: int
    pronunciation_avg: float


class PronunciationReport(BaseModel):
    """T8 통화별 발음 상세 응답."""

    call_id: int
    country: Optional[str] = None
    sentences: list[PronSentenceScore]
    sounds: list[SoundAggregate]
    comment: Optional[str] = None


class PronHistoryItem(BaseModel):
    """T9 최근 통화 1건의 발음 추이 항목."""

    call_id: int
    call_date: Optional[datetime] = None
    sentence_count: int
    score: Optional[float] = None
