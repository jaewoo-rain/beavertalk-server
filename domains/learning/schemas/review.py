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
    """⛔⛔ §2(2026-09-27, 앱 요청) — 네 칸 다 Optional. 채점을 못 했으면(evaluation
    이 아예 없는 sentence, 운영 2,956건 중 2,870건=97%) 0 이 아니라 null 을 보낸다 —
    0 은 "0점을 받았다"는 뜻이라 다르다. Q9(발음 리포트 score: int|None)와 같은 판단."""

    total_score: Optional[int] = None
    pronunciation: Optional[int] = None
    fluency: Optional[int] = None
    rhythm: Optional[int] = None


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
    # ⭐ §3(2026-09-27, 앱 요청) — 키 없음·로드 실패 등으로 결정적 스텁(60~100)이
    # 나갔다는 표식. 이 필드가 생기기 전의 옛 복습 행은 False(구분 불가, 실채점 취급).
    is_stub: bool = False
