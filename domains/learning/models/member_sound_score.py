"""member_sound_score (회원별 소리 점수) — learning 도메인.

취약 발음 목록에 찍히는 점수의 **단일 출처**. 회원 × 소리 1행이고, 평가 단계(4단계)
제출로만 갱신된다. 연습 단계(단어·문장)는 무채점이라 이 테이블을 건드리지 않는다.

`aggregate_sounds()`(복습 집계)와 무엇이 다른가 — 집계는 **통화 복습에서 저절로 나온**
소리 정확도고, 이 테이블은 **학습을 해서 바꾼** 점수다. 목록은 이 행이 있으면 이 값을,
없으면 집계값을 보여준다. 그래서 학습 전 점수(집계)와 학습 후 점수(이 테이블)가 같은
화면에서 62 → 84 로 이어진다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class MemberSoundScore(Base, TimestampMixin):
    __tablename__ = "member_sound_score"
    __table_args__ = (
        # 회원당 소리 1행 — 멱등 upsert 의 근거. 없으면 평가를 두 번 내면 행이 둘 된다.
        UniqueConstraint("member_id", "sound_key", name="uq_member_sound_score"),
    )

    member_sound_score_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE"), index=True, nullable=False,
    )
    sound_key: Mapped[str] = mapped_column(
        Text, nullable=False, comment="소리 키 — sound_lesson.sound_key 와 같은 값",
    )
    score: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="최신 평가 점수 0~100",
    )
    best_score: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="역대 최고 점수 — 재도전으로 점수가 내려가도 성취는 남긴다",
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", comment="평가 제출 누적 횟수",
    )
    baseline_score: Mapped[Optional[int]] = mapped_column(
        Integer, comment="첫 학습 직전 점수(복습 집계값). 결과 화면의 「학습 전」 막대",
    )
    last_learned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="마지막 평가 제출 시각",
    )
