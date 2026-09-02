"""assignment (과제) — 반 + TOPIK 챕터 1개 + 활동 1~3종 + 마감.

제목을 교사가 쓰지 않는다. 시스템이 `TOPIK {급수} · Chapter {NN}` 로 조립한다
(`03_과제체계.md` §2). 교사 자유입력 텍스트는 학습자가 못 읽기 때문이다
(`10_다국어_앱화면.md` §4) — 그래서 메모 필드도 P0 에서 뺐다.

- `activities` : `['speaking','conversation','workbook']` 부분집합. TEXT(JSON).
  프로젝트 컨벤션상 테스트가 sqlite 라 JSONB 를 쓰지 않는다.
- `target_item_ids` : **출제 시점 스냅샷**. 챕터에서 자동으로 가져온 뒤 교사가 제외한
  결과를 굳힌다. 커리큘럼이 나중에 바뀌어도 이미 낸 과제는 그대로여야 한다.
- `grammar_items` : 같은 이유의 문법 스냅샷.
  🔴 표제(`surface`)만 담는다. `learning_item.examples` 는 **서울대 한국어 교재 예문**이라
  화면에 띄우지 않는다(`07_데이터출처.md`). 학습자에게 나가는 예문은 자체 LLM 생성분이다.
- 회화 과제는 `target_item_ids` 를 통화 프롬프트의 목표 slot 에 주입한다(`06_회화설계.md` §2).
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
    from domains.classroom.models.classroom import Classroom
    from domains.classroom.models.submission import Submission


class Assignment(Base, TimestampMixin):
    __tablename__ = "assignment"
    __table_args__ = (
        UniqueConstraint(
            "classroom_id", "grade", "chapter", "created_at", name="uq_assignment_slot"
        ),
        CheckConstraint("grade BETWEEN 1 AND 6", name="ck_assignment_grade"),
        CheckConstraint("chapter > 0", name="ck_assignment_chapter"),
        Index("ix_assignment_classroom_due", "classroom_id", "due_at"),
    )

    assignment_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    classroom_id: Mapped[int] = mapped_column(
        ForeignKey("classroom.classroom_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    grade: Mapped[int] = mapped_column(Integer, nullable=False, comment="TOPIK 급수(1~6)")
    chapter: Mapped[int] = mapped_column(Integer, nullable=False, comment="챕터 번호")

    activities: Mapped[str] = mapped_column(
        Text, nullable=False, comment="활동 JSON 배열(speaking/conversation/workbook)"
    )
    target_item_ids: Mapped[str] = mapped_column(
        Text, nullable=False, comment="출제 시점 학습 항목 id 스냅샷(JSON 배열)"
    )
    grammar_items: Mapped[Optional[str]] = mapped_column(
        Text, comment="문법 표제 스냅샷(JSON 배열) · 교재 예문은 담지 않는다"
    )

    due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="마감 시각"
    )
    reminder_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="마감 전날 자동 알림 발송 시각"
    )
    # ⛔ 위 컬럼과 합치지 마라. 교사가 손으로 보내는 알림은 **자동 알림과 별개**로
    #    한 번 더 나간다(콘솔 D8 문안이 그렇게 약속한다). 한 칸을 공유하면 손으로
    #    보낸 순간 마감 전날 자동 알림이 사라진다.
    manual_reminder_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="교사가 손으로 보낸 알림의 마지막 발송 시각"
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="마감 처리 시각(NULL=진행 중)"
    )
    # 워크북은 앱 안에서 열지 않는다 — 교사가 올린 PDF 의 외부 링크(Google Drive 등)를
    # 그대로 보관하고 앱은 브라우저로 넘긴다. 뷰어를 들이면 30 로케일 폰트가 따라온다.
    # ⛔ 서버가 파일을 보관하지 않으므로 접근권한은 링크 주인(교사)의 책임이다.
    workbook_url: Mapped[Optional[str]] = mapped_column(
        Text, comment="워크북 PDF 외부 링크(교사가 입력)"
    )

    classroom: Mapped["Classroom"] = relationship(
        back_populates="assignments", lazy="select"
    )
    submissions: Mapped[list["Submission"]] = relationship(
        back_populates="assignment", lazy="select", cascade="all, delete-orphan"
    )
