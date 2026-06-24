"""call/sentence/evaluation/raw_data DTO."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class CallCharacterBrief(BaseModel):
    character_id: int
    name: str
    image_url: Optional[str]


# ── 입력(통화 일괄 저장) ──
class EvaluationIn(BaseModel):
    """발화별 평가 점수. 채점 전이면 전부 생략(placeholder) 가능."""

    total_score: Optional[int] = None
    pronunciation: Optional[int] = None
    fluency: Optional[int] = None
    rhythm: Optional[int] = None


class SentenceIn(BaseModel):
    korean_sentence: Optional[str] = None
    native_sentence: Optional[str] = None
    locale: Optional[str] = None
    voice_url: Optional[str] = None
    is_bookmarked: bool = False
    evaluation: EvaluationIn = Field(default_factory=EvaluationIn)


class RawDataIn(BaseModel):
    content: Optional[str] = None
    voice_url: Optional[str] = None
    total_time: Optional[int] = None


class CallCreate(BaseModel):
    """통화 한 건 전체(발화·평가·원본 포함)를 한 번에 저장."""

    character_id: int
    call_date: Optional[datetime] = None
    total_time: Optional[int] = None
    summary: Optional[str] = None
    rating: Optional[int] = Field(default=None, ge=1, le=3)
    sentences: list[SentenceIn] = Field(default_factory=list)
    raw_data: list[RawDataIn] = Field(default_factory=list)


class CallRatingUpdate(BaseModel):
    rating: int = Field(ge=1, le=3)


# ── 출력 ──
class EvaluationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    evaluation_id: int
    total_score: Optional[int]
    pronunciation: Optional[int]
    fluency: Optional[int]
    rhythm: Optional[int]


class SentenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sentence_id: int
    korean_sentence: Optional[str]
    native_sentence: Optional[str]
    locale: Optional[str]
    voice_url: Optional[str]
    is_bookmarked: Optional[bool]
    evaluation: Optional[EvaluationOut]


class RawDataOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    call_raw_data_id: int
    content: Optional[str]
    voice_url: Optional[str]
    total_time: Optional[int]


class CallSummary(BaseModel):
    """목록용 — 얕게(발화 미포함)."""

    call_id: int
    call_date: Optional[datetime]
    total_time: Optional[int]
    summary: Optional[str]
    rating: Optional[int]
    character: CallCharacterBrief


class CallDetail(CallSummary):
    """상세용 — 발화+평가 중첩."""

    sentences: list[SentenceOut]


# ── 통화 분석 결과(종료 후) ──
class ScoreAverage(BaseModel):
    """통화 내 모든 발화 평가의 평균(점수 없는 발화는 제외)."""

    total_score: Optional[float]
    pronunciation: Optional[float]
    fluency: Optional[float]
    rhythm: Optional[float]


class CallResultSentence(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sentence_id: int
    korean_sentence: Optional[str]
    native_sentence: Optional[str]
    voice_url: Optional[str]
    is_bookmarked: Optional[bool]


class CallResult(BaseModel):
    """통화 종료 후 결과 화면 — 평균 점수 + 사용된 문장 전체."""

    call_id: int
    summary: Optional[str]
    rating: Optional[int]
    average: ScoreAverage
    sentences: list[CallResultSentence]
