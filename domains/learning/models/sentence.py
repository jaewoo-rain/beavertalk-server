"""sentence (발화) — learning 도메인.

call 의 자식(N:1), evaluation 의 부모(1:1), review 의 부모(1:N).
deleted_at 으로 소프트 삭제(개별 문장 삭제는 하드 삭제하지 않음).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.learning.models.call import Call
    from domains.learning.models.evaluation import Evaluation
    from domains.learning.models.review import Review


class Sentence(Base, TimestampMixin):
    __tablename__ = "sentence"

    sentence_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    call_id: Mapped[int] = mapped_column(
        ForeignKey("call.call_id", ondelete="CASCADE"), index=True, comment="통화",
    )
    korean_sentence: Mapped[Optional[str]] = mapped_column(Text, comment="한국어 문장")
    native_sentence: Mapped[Optional[str]] = mapped_column(Text, comment="모국어 문장")
    locale: Mapped[Optional[str]] = mapped_column(Text, comment="언어")
    source_type: Mapped[Optional[str]] = mapped_column(
        Text, comment="표현 출처(asked/corrected/drilled)",
    )
    voice_url: Mapped[Optional[str]] = mapped_column(Text, comment="보이스 데이터 저장 위치")
    is_bookmarked: Mapped[Optional[bool]] = mapped_column(Boolean, comment="북마크 여부")
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), index=True, comment="소프트 삭제 시각(NULL=정상)",
    )
    # ⭐⭐ C9(2026-09-23, docs/plans/2026-09-22-프리미엄-자유대화-15분-달력.md) — 현지인
    #   표현 짝 행. **기본 문장은 이 세 칸이 전부 NULL** 이다 — 현지인 행만 값을 가진다
    #   (같은 테이블에 "짝" 개념을 얹은 것이지 새 종류의 문장이 아니다).
    kind: Mapped[Optional[str]] = mapped_column(
        Text, comment="NULL=기본 문장 · 'native'=현지인 표현 짝",
    )
    paired_sentence_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("sentence.sentence_id", ondelete="SET NULL", name="fk_sentence_paired"),
        comment="kind='native' 인 행이 가리키는 기본 문장(NULL=기본 문장 자신)",
    )
    nuance: Mapped[Optional[str]] = mapped_column(
        Text, comment="현지인 표현의 뉘앙스 한 줄(모국어, kind='native' 에만 값)",
    )

    call: Mapped["Call"] = relationship(back_populates="sentences")
    # 1:1 평가(자식). 발화 삭제 시 평가도 함께(delete-orphan + DB CASCADE)
    evaluation: Mapped[Optional["Evaluation"]] = relationship(
        back_populates="sentence",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    reviews: Mapped[list["Review"]] = relationship(
        back_populates="sentence", cascade="all, delete-orphan", passive_deletes=True,
    )
