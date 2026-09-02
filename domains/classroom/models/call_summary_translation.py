"""call_summary_translation — 통화 요약의 교사 로케일 번역 캐시.

**문제** (`10_다국어_앱화면.md` §12.7):
`call.summary` 는 통화 시점에 **학습자 로케일로 1회만** 생성된다. 베트남 학습자의 요약은
베트남어로 남고, 교사는 영어 콘솔에서 그것을 읽을 수 없다.

**선택지 3개와 대가**

| 안 | 대가 |
|---|---|
| 통화 시점에 2언어 동시 산출 | 저장 2배 + **통화 시점에 콘솔 언어를 알아야 함**(모른다) |
| 한국어 고정 | 영어 콘솔을 채택한 이상 무의미 |
| **교사가 열 때 1회 번역·캐시** ← 채택 | 첫 열람에 LLM 1콜. 이후 0 |

채택 근거 — 통화 시점 비용이 0 이고, 실제로 읽는 요약만 번역한다.
교사가 안 여는 요약은 영원히 번역되지 않는다.

⛔ `call.summary` 를 덮어쓰지 않는다. 원본은 학습자 것이다.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Identity,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class CallSummaryTranslation(Base, TimestampMixin):
    __tablename__ = "call_summary_translation"
    __table_args__ = (
        UniqueConstraint("call_id", "locale", name="uq_call_summary_translation"),
    )

    call_summary_translation_id: Mapped[int] = mapped_column(
        BigInteger, Identity(), primary_key=True
    )

    call_id: Mapped[int] = mapped_column(
        ForeignKey("call.call_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    locale: Mapped[str] = mapped_column(
        Text, nullable=False, comment="번역 대상 로케일(콘솔 언어)"
    )
    text: Mapped[str] = mapped_column(Text, nullable=False, comment="번역된 요약")
    source_locale: Mapped[Optional[str]] = mapped_column(
        Text, comment="원본 요약의 로케일(학습자 모국어)"
    )
