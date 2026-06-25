"""level (한국어 레벨) — learning 도메인. 마스터 데이터(12단계).

member.korean_level(1~12)이 가리키는 레벨 마스터. 통화 프롬프트의 [학습자 수준]
슬롯에 주입할 발화 프로파일(profile)과 핵심 문법(grammar_scope)·대표 어휘(vocab_sample)를
담는다. 원본 전체 문법/어휘는 assets/level/{grammar,vocab}_12levels.json 레퍼런스.
시드 소스: assets/level/level_profiles_12.json (한국어_단계별_문법·어휘_12단계.xlsx 추출).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import BigInteger, Identity, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class Level(Base, TimestampMixin):
    __tablename__ = "level"
    __table_args__ = (
        UniqueConstraint("level_no", name="uq_level_no"),
    )

    level_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    level_no: Mapped[int] = mapped_column(Integer, comment="레벨 번호(1~12)")
    band: Mapped[Optional[str]] = mapped_column(Text, comment="밴드(초급/중급/고급)")
    grade: Mapped[Optional[str]] = mapped_column(Text, comment="어휘 등급(A/B/C)")
    stage_name: Mapped[Optional[str]] = mapped_column(Text, comment="단계명(초급 1 …)")
    textbook: Mapped[Optional[str]] = mapped_column(Text, comment="교재명(Basic Korean A …)")
    grammar_count: Mapped[Optional[int]] = mapped_column(Integer, comment="문법 포인트 수")
    vocab_count: Mapped[Optional[int]] = mapped_column(Integer, comment="어휘 수")
    grammar_scope: Mapped[Optional[str]] = mapped_column(Text, comment="핵심 문법(JSON 배열 문자열)")
    vocab_sample: Mapped[Optional[str]] = mapped_column(Text, comment="고빈도 대표 어휘(JSON 배열 문자열)")
    profile: Mapped[Optional[str]] = mapped_column(
        Text, comment="발화 프로파일(프롬프트 [학습자 수준] 슬롯 주입)"
    )
