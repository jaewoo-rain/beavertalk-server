"""발음 리포트 스키마 — 복습 종료 후 화면(Flutter `LearningSummary`) 계약.

Flutter `lib/screens/home/learning_summary.dart` 의 `LearningSummary` 를 그대로 맞춘다.
JSON 키는 기존 DTO 컨벤션(snake_case). 클라가 이 모양으로 파싱한다.

데이터는 전부 실집계 — pronunciation_report_service.build_learning_summary 가 main 의
pronunciation_service(문장별 점수·자모별 소리 집계·국가 맞춤 코칭 comment·국적) + 발음
이력을 받아 이 형태로 가공한다(통과수·평균·가장 어려웠던 소리·소리별 정확도 2+2 선별·
세션 delta). 클래스명이 main 의 PronunciationReport 와 겹치지 않게 LearningSummaryOut.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class PhonemeStatOut(BaseModel):
    """소리별 정확도 한 줄(음소 단위). accuracy 는 클라가 correct/attempts 로 계산."""

    sound: str      # 학습자에게 보이는 소리 라벨, 예: "받침 ㄹ", "ㅓ / ㅗ 구분"
    attempts: int   # 그 소리가 나온 문장 수(문장 1회 카운트)
    correct: int    # 그중 정확히 발음한 수


class SentenceScoreOut(BaseModel):
    """문장별 결과 한 줄."""

    sentence: str
    pronunciation: int
    fluency: int
    rhythm: int


class SessionPointOut(BaseModel):
    """최근 세션 한 점(그래프 막대 + 표 한 줄). oldest first."""

    label: str          # 그래프 x축, 예: "12/21" 또는 "오늘"
    date: str           # 표 날짜칸, 예: "12월 21일"
    sentences: int      # 그 세션 문장 수
    score: int          # 0~100 세션 점수
    delta: int | None = None  # 직전 세션 대비 변화(가장 오래된 것은 null → "—")


class LearningSummaryOut(BaseModel):
    """복습 종료 후 발음 리포트 전체(= Flutter LearningSummary).

    main 의 발음/국적 기능(pronunciation_service)이 낸 실데이터를 이 형태로 가공한다
    (pronunciation_report_service.build_learning_summary). 클래스명이 main 의
    PronunciationReport 와 겹치지 않도록 LearningSummaryOut 로 둔다.
    """

    passed: int
    total: int
    date: datetime
    overall: int
    pronunciation: int
    fluency: int
    rhythm: int
    hardest_sound: str
    hardest_evidence: str
    l1_interference: str
    phonemes: list[PhonemeStatOut]
    sentences: list[SentenceScoreOut]
    sessions: list[SessionPointOut]
