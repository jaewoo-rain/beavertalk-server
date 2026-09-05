"""classroom_member (반 명단) — 학습자가 반에 들어온 기록.

★ 이름이 둘이라는 것이 이 테이블의 핵심이다(`04_학습자관리.md` §2).

| 이름 | 소유자 | 어디에 보이나 |
|---|---|---|
| `member.name` | 학습자 | 앱 |
| `classroom_member.roster_name` | 교사(가 확인) | 콘솔 명단 |

학습자는 앱에서 `마리아` 로 쓰더라도, 반 참여 시 출석부와 맞출 이름을 **직접**
적는다. 교사는 그 이름만 본다. 이메일·국적·모국어·앱 이름은 콘솔에 노출하지 않는다
(`04` §5 개인정보 경계).

- `roster_name` NOT NULL : 이름 없는 명단 행을 만들지 않는다. 교사가 누군지 못 알아본다.
- `confirmed_at` : 교사가 출석부와 대조해 확인한 시각. NULL 이면 명단에 '미확인'.
- `left_at` : 반에서 내보냄(소프트). 개인 결과 표시는 사라지지만 반 평균 집계에는 남는다
  — 하드 삭제하면 이미 산출한 평균이 소급해서 바뀐다.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.account.models.member import Member
    from domains.classroom.models.classroom import Classroom
    from domains.classroom.models.submission import Submission


class ClassroomMember(Base, TimestampMixin):
    __tablename__ = "classroom_member"
    __table_args__ = (
        UniqueConstraint("classroom_id", "member_id", name="uq_classroom_member"),
        Index("ix_classroom_member_active", "classroom_id", "left_at"),
    )

    classroom_member_id: Mapped[int] = mapped_column(
        BigInteger, Identity(), primary_key=True
    )

    classroom_id: Mapped[int] = mapped_column(
        ForeignKey("classroom.classroom_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 🔴 이탈 시 NULL 이 된다(`remove_from_class`). 사람으로 가는 유일한 링크라
    #    개인정보처리방침 §3.5 ⑤ 「철회 즉시 교사가 조회할 수 없다」의 실현 수단이다.
    member_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE"),
        index=True,
        comment="이탈 시 NULL — 사람으로 가는 링크를 끊는다",
    )

    roster_name: Mapped[Optional[str]] = mapped_column(
        Text, comment="반에서 쓸 이름(학습자 입력) · 이탈 시 NULL 로 파기"
    )
    student_no: Mapped[Optional[str]] = mapped_column(
        Text, comment="학번(동명이인 구분용 · 선택)"
    )
    teacher_alias: Mapped[Optional[str]] = mapped_column(
        Text, comment="교사 메모용 별칭(한글 음차 등 · 콘솔 전용)"
    )

    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="교사 명단 확인 시각(NULL=미확인)"
    )
    left_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), index=True, comment="반에서 내보낸 시각(소프트)"
    )

    classroom: Mapped["Classroom"] = relationship(back_populates="members", lazy="select")
    member: Mapped["Member"] = relationship(lazy="select")
    submissions: Mapped[list["Submission"]] = relationship(
        back_populates="classroom_member", lazy="select", cascade="all, delete-orphan"
    )
