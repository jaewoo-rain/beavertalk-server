"""classroom (반) — B2B 숙제 기능의 최상위 단위.

교사 1명이 반 N개를 가진다. 조직→반→좌석 3단이 아니라 **반 1단**이다
(`02_기능정의.md` 절감 #2) — `teacher_member_id` 로 교사에 직결한다.

- `join_code` : 학습자가 앱에 입력하는 6자리 코드. 교사가 판서로 배포한다.
  charset 에서 `I·O·0·1` 을 뺀다 — 손글씨로 옮겨 적을 때 서로 오인된다
  (`05_데이터모델.md` §3). 유출 방어선은 `capacity` 와 `code_expires_on` 이다.
- `name` : 교사가 직접 쓴 고유명사. 학습자 앱에도 이대로 보인다.
  **어느 로케일에서도 번역하지 않는다**(`10_다국어_앱화면.md` §4).
- `target_grade` : TOPIK 1~6. 과제를 낼 챕터의 출처가 된다. 반 만들기 화면이
  급수를 정하는 유일한 지점이다(`03_과제체계.md` §6).
- `archived_at` : 학기 종료. 값이 있으면 새 과제를 낼 수 없고 코드가 막힌다.
  지난 과제와 결과는 그대로 남는다(하드 삭제 아님).

설계: `23_제품기획_하네스/_output/2026-08-19_비버톡_B2B숙제/05_데이터모델.md`
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
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


class Classroom(Base, TimestampMixin):
    __tablename__ = "classroom"
    __table_args__ = (
        UniqueConstraint("join_code", name="uq_classroom_join_code"),
        CheckConstraint(
            "target_grade BETWEEN 1 AND 6", name="ck_classroom_target_grade"
        ),
        CheckConstraint("capacity > 0", name="ck_classroom_capacity"),
        Index("ix_classroom_teacher_archived", "teacher_member_id", "archived_at"),
    )

    classroom_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    teacher_member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="담당 교사(member.is_teacher=true)",
    )

    name: Mapped[str] = mapped_column(
        Text, nullable=False, comment="반 이름(교사 자유입력 · 번역 금지)"
    )
    target_grade: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="목표 TOPIK 급수(1~6)"
    )
    term: Mapped[Optional[str]] = mapped_column(Text, comment="학기 라벨(예: 2026 가을학기)")

    join_code: Mapped[str] = mapped_column(
        Text, nullable=False, comment="참여코드 6자리(I·O·0·1 제외)"
    )
    capacity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, comment="반 정원(코드 유출 방어선)"
    )
    code_expires_on: Mapped[Optional[date]] = mapped_column(
        Date, comment="참여코드 만료일(NULL=무기한)"
    )

    institution: Mapped[Optional[str]] = mapped_column(
        Text, comment="기관명(학습자가 참여 시 확인)"
    )
    teacher_display_name: Mapped[Optional[str]] = mapped_column(
        Text, comment="학습자에게 보이는 교사명"
    )

    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), index=True, comment="보관 시각(NULL=운영 중)"
    )

    members: Mapped[list["ClassroomMember"]] = relationship(
        back_populates="classroom", lazy="select", cascade="all, delete-orphan"
    )
    assignments: Mapped[list["Assignment"]] = relationship(
        back_populates="classroom", lazy="select", cascade="all, delete-orphan"
    )
