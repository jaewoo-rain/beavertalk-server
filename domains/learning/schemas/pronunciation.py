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


class PronSummaryOut(BaseModel):
    """마이페이지 발음 분석 카드 — 최근 N세션 평균.

    ⭐ 값의 출처는 **SpeechSuper 실채점**이다(2026-08-28 확인). 계정 만료(errId 41030)로
    한동안 스텁 폴백이었는데 재연동됐다 — 실측: `sent.eval.kr` 호출 성공, overall 93 ·
    pronunciation 94 · fluency 87 · rhythm 87, words 13개 · phonemes 30개(`phoneme` 에 실제
    자모값). 설계대로 **스키마·계산은 한 줄도 안 바꾸고** 값만 진짜로 바뀌었다.
    ⚠ 키가 없거나 벤더가 죽으면 `core.speechsuper` 가 **조용히 스텁으로 되돌아간다**
      (예외를 안 던진다). 그래서 「점수가 나온다」는 실채점의 증거가 아니다 —
      가르려면 `_stub_assess` 와 대조하거나 `phoneme` 필드가 빈 문자열인지 봐라.

    Attributes:
        sessions: 평균에 실제로 들어간 통화 수(요청한 N 이하). 0이면 아직 발음
            기록이 없다는 뜻이고, 이때 점수는 전부 None 이다.
        sentence_count: 평균에 들어간 문장 수(표본 크기). 세션 수보다 이게 신뢰도를
            더 잘 나타내서 같이 준다.
        total_score/pronunciation/fluency/rhythm: 소수 1자리 반올림. 표본이 없으면 None.
    """

    sessions: int
    sentence_count: int
    total_score: Optional[float] = None
    pronunciation: Optional[float] = None
    fluency: Optional[float] = None
    rhythm: Optional[float] = None


class PronHistoryItem(BaseModel):
    """T9 최근 통화 1건의 발음 추이 항목."""

    call_id: int
    call_date: Optional[datetime] = None
    sentence_count: int
    score: Optional[float] = None
