"""level (한국어 레벨) — learning 도메인. 마스터 데이터(13단계).

member.korean_level(1~13)이 가리키는 레벨 마스터(레벨 1=생존 회화). 통화 프롬프트의 [학습자 수준]
슬롯에 주입할 발화 프로파일(profile)과 밴드·단계명 메타를 담는다.
원본 전체 문법/어휘는 learning_item(assets/level/curriculum_v2/) 레퍼런스.
시드 소스: assets/level/level_profiles_13.json.

(D15) grammar_count/vocab_count/grammar_scope/vocab_sample 은 폐지 — 런타임 사용처 0,
learning_item 이 레벨별 문법·어휘의 단일 소스라 잉여 캐시였다.
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
    level_no: Mapped[int] = mapped_column(Integer, comment="레벨 번호(1~13, 1=생존 회화)")
    band: Mapped[Optional[str]] = mapped_column(Text, comment="밴드(초급/중급/고급)")
    grade: Mapped[Optional[str]] = mapped_column(Text, comment="어휘 등급(A/B/C)")
    stage_name: Mapped[Optional[str]] = mapped_column(Text, comment="단계명(초급 1 …)")
    textbook: Mapped[Optional[str]] = mapped_column(Text, comment="교재명(Basic Korean A …)")
    profile: Mapped[Optional[str]] = mapped_column(
        Text, comment="발화 프로파일(프롬프트 [학습자 수준] 슬롯 주입)"
    )
