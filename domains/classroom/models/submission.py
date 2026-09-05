"""submission (제출) — 학습자 1명이 과제 1건을 수행한 결과.

집계는 여기서 새로 계산하지 않는다. **기존 체크판·증거 로그를 과제 id 로 묶을 뿐이다**
(`06_회화설계.md` §4) — `member_item_progress` 와 append-only `item_evidence` 가
이미 어떤 항목이 등장했는지 증거로 남긴다.

- `speaking_passed` : 발음 과제에서 AI 가 알아들은 문장 수.
  🔴 발음 **점수**가 아니다. `learning_intro` 계열 화면이 서버 채점을 하고
  `GET /calls/{id}/pronunciation-report` 가 실집계를 낸다(`10` §7.2).
- `conversation_met` : 자유 회화에서 실제로 **쓴** 목표 표현 수.
  E2·E3 증거만 센다. E1(모방)은 세지 않는다 — 비버가 방금 한 말을 따라한 것은
  사용이 아니다(`06` §4).
- `call_id` : 회화 과제를 수행한 통화. 요약·분석의 출처다.

★ `status='done'` 의 기준은 **시간도 턴 수도 아니다 — 증거 유무다.**
  「증거통화」= `item_evidence` 에 행이 있는 distinct call
  (`learning/repository/mastery_repository.py`). `call.is_valid_call` 은 D15 로 폐지됐다.
  30초 통화라도 항목이 하나 잡히면 수행이고, 5분을 채웠어도 0건이면 미수행이다.
  **출석이 아니라 산출을 센다.** 제출 배선을 붙일 때 시간 임계를 새로 만들지 마라.

⛔ 발음 챌린지(`pronunciation_challenge`)는 서버에 결과를 보내지 않는다. 여기 들어올 수
   없다 — 그 화면은 과제 수행이 아니라 완료 후 선택 복습이다(`10` §7.3).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.classroom.models.assignment import Assignment
    from domains.classroom.models.classroom_member import ClassroomMember


class Submission(Base, TimestampMixin):
    __tablename__ = "submission"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id", "classroom_member_id", name="uq_submission_member"
        ),
        CheckConstraint(
            "status IN ('not_started', 'in_progress', 'done')",
            name="ck_submission_status",
        ),
        Index("ix_submission_assignment_status", "assignment_id", "status"),
    )

    submission_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("assignment.assignment_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    classroom_member_id: Mapped[int] = mapped_column(
        ForeignKey("classroom_member.classroom_member_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="not_started", comment="not_started/in_progress/done"
    )

    speaking_passed: Mapped[Optional[int]] = mapped_column(
        Integer, comment="AI 가 알아들은 문장 수(점수 아님)"
    )
    speaking_total: Mapped[Optional[int]] = mapped_column(Integer, comment="출제 문장 수")
    conversation_met: Mapped[Optional[int]] = mapped_column(
        Integer, comment="회화에서 실제로 쓴 목표 표현 수(E2·E3만)"
    )
    conversation_total: Mapped[Optional[int]] = mapped_column(
        Integer, comment="목표 표현 수"
    )

    call_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("call.call_id", ondelete="SET NULL"),
        index=True,
        comment="회화 과제를 수행한 통화",
    )
    failed_item_ids: Mapped[Optional[str]] = mapped_column(
        Text, comment="미통과 항목 id(JSON 배열) · 취약 문장 집계용"
    )

    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="수행 완료 시각"
    )

    assignment: Mapped["Assignment"] = relationship(
        back_populates="submissions", lazy="select"
    )
    classroom_member: Mapped["ClassroomMember"] = relationship(
        back_populates="submissions", lazy="select"
    )
