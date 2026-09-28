"""member_language_level (회원×언어별 현재 레벨) — learning 도메인. (멀티랭귀지)

한 회원이 여러 target 언어(ko/en/ja/zh/fr/vi)를 동시에 학습하므로, "현재 레벨"은
언어 축으로 분리돼야 한다. member.korean_level(단일 스칼라)의 다국어 일반화 —
ko 행은 member.korean_level 과 dual-read/write 로 폴백 정합을 유지한다(T3).

- UNIQUE(member_id, language) : 회원×언어당 정확히 1행(현재 레벨 상태).
- level_no NULLABLE : 새 언어의 콜드스타트(레벨테스트 미실시) 상태 = 행은 있으나
  level_no NULL. 복합 FK 는 MATCH SIMPLE 기본이라 level_no NULL 이면 FK 미검사 →
  "아직 레벨 없음"을 자연스럽게 허용한다. 레벨테스트가 placement 로 값을 채운다.
- 복합 FK (language, level_no) → level(language, level_no) RESTRICT :
  존재하는 언어별 레벨만 가리킨다. 레벨 마스터 행은 회원이 매달린 채 못 지운다.
- 레벨 진입 시각은 여기 두지 않는다 — member_level_history(언어별) 최신 행 created_at
  이 단일 소스(이중 소스 금지). 이 테이블은 "현재 상태"만.

설계: docs/plans/2026-07-20-multi-language-platform.md §3 T1·T3.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class MemberLanguageLevel(Base, TimestampMixin):
    __tablename__ = "member_language_level"
    __table_args__ = (
        # 회원×언어당 현재 레벨 1행
        UniqueConstraint("member_id", "language", name="uq_member_language"),
        # 언어별 레벨 마스터 참조(level_no NULL = 콜드스타트, FK 미검사)
        ForeignKeyConstraint(
            ["language", "level_no"],
            ["level.language", "level.level_no"],
            ondelete="RESTRICT",
            name="fk_mll_level",
        ),
    )

    language_level_id: Mapped[int] = mapped_column(
        BigInteger, Identity(), primary_key=True,
    )
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE", name="fk_mll_member"),
        comment="회원",
    )
    language: Mapped[str] = mapped_column(
        Text, comment="학습 대상 언어(ISO 639-1)",
    )
    level_no: Mapped[Optional[int]] = mapped_column(
        Integer, comment="현재 레벨(1~13 → level.level_no, NULL=콜드스타트/레벨테스트 미실시)",
    )
    retest_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        comment=(
            "§9(2026-09-28) 「다시하기」 요청 시각(NULL=대기 없음). 요청만으론 이 행을 "
            "지우지 않는다 — 실제 초기화(행 삭제·korean_level NULL)는 레벨테스트 통화가 "
            "call_started 로 성립한 뒤에 한다(mastery_service.apply_pending_retest). "
            "즉시 지우면 재측정 성공과 통화 성립 사이 취소·거절(ALREADY_IN_CALL·"
            "DAILY_LIMIT)에서 레벨만 사라진 채 남는다."
        ),
    )
