"""cur_* 저장소 — **순수 SELECT**(commit 없음, R3). 트랜잭션 경계는 service/curriculum_service.py 가 갖는다.

계획 docs/plans/2026-09-12-cur-2단계-통화경로-이전.md §2(데이터 흐름)·§7(P1-1~3) · 선별 규칙 docs/20260912_0330_cur-스키마-설계.md §11.

⛔⛔ 원칙 — 이 파일은 **cur_* + member + call** 만 읽는다. 옛 학습 테이블(목록은 models/curriculum.py 독스트링)은
  이름조차 적지 않는다 — 시험이 grep 으로 0건을 잠근다(계획 §4 «옛 테이블 미참조»).
⚠ sqlite(시험)·Postgres(운영) 둘 다 돌아야 한다 — 창 함수(row_number)·random() 은 둘 다 있다. FOR UPDATE 는 Postgres 에서만 건다(§6 ②).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from domains.learning.models.call import Call
from domains.learning.models.curriculum import (
    CurCall,
    CurItem,
    CurLesson,
    CurLessonItem,
    CurMemberItem,
    CurMemberLesson,
    CurMemberProgress,
)


def _is_postgres(db: Session) -> bool:
    bind = db.get_bind()
    return bool(bind is not None and bind.dialect.name == "postgresql")


# ── 진도(포인터) ──────────────────────────────────────────────────────────── #
def current_progress(db: Session, member_id: int, language: str = "ko", *, for_update: bool = False) -> Optional[CurMemberProgress]:
    """회원의 «지금 차시» 행. for_update 면 Postgres 에서 행 잠금(동시 start 방어, §6 ②) — sqlite 는 무시."""
    stmt = select(CurMemberProgress).where(
        CurMemberProgress.member_id == member_id, CurMemberProgress.language == language
    )
    if for_update and _is_postgres(db):
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def lesson_by_no(db: Session, language: str, no: int) -> Optional[CurLesson]:
    return db.execute(
        select(CurLesson).where(CurLesson.language == language, CurLesson.no == no)
    ).scalar_one_or_none()


def lesson_by_id(db: Session, lesson_id: int) -> Optional[CurLesson]:
    return db.get(CurLesson, lesson_id)


def next_lesson(db: Session, language: str, after_no: int) -> Optional[CurLesson]:
    """현재 뒤의 첫 차시 = MIN(no) > after_no. 없으면 None(마지막 차시 — 포인터 유지)."""
    return db.execute(
        select(CurLesson)
        .where(CurLesson.language == language, CurLesson.no > after_no)
        .order_by(CurLesson.no.asc())
        .limit(1)
    ).scalar_one_or_none()


# ── 차시 항목 ─────────────────────────────────────────────────────────────── #
def lesson_items(db: Session, lesson_id: int) -> list[tuple[CurLessonItem, CurItem]]:
    """그 차시가 가르치는 항목 — seq 순(문법→필수→핵심→지원 / 청크), **은퇴 항목 제외**(P1-3)."""
    rows = db.execute(
        select(CurLessonItem, CurItem)
        .join(CurItem, CurItem.item_id == CurLessonItem.item_id)
        .where(CurLessonItem.lesson_id == lesson_id, CurItem.retired_at.is_(None))
        .order_by(CurLessonItem.seq.asc())
    ).all()
    return [(li, it) for li, it in rows]


def lesson_item_role(db: Session, lesson_id: int, item_id: int) -> Optional[str]:
    """그 차시에서 항목의 role(복습 DTO 용)."""
    return db.execute(
        select(CurLessonItem.role).where(CurLessonItem.lesson_id == lesson_id, CurLessonItem.item_id == item_id)
    ).scalar_one_or_none()


def member_item_map(db: Session, member_id: int, lesson_id: int) -> dict[int, CurMemberItem]:
    """(회원, 차시) 의 항목 기록 — item_id → 행."""
    rows = db.execute(
        select(CurMemberItem).where(CurMemberItem.member_id == member_id, CurMemberItem.lesson_id == lesson_id)
    ).scalars().all()
    return {r.item_id: r for r in rows}


def member_item(db: Session, member_id: int, lesson_id: int, item_id: int) -> Optional[CurMemberItem]:
    return db.get(CurMemberItem, (member_id, lesson_id, item_id))


def review_pool(
    db: Session, member_id: int, exclude_item_ids: set[int] | frozenset[int], limit: int,
) -> list[tuple[CurMemberItem, CurItem]]:
    """복습 풀(§11 ②) — 그 회원이 **이미 배운**(drilled_at NOT NULL) 항목, 어느 차시든.

    · item_id 로 DISTINCT — 두 차시에 속한 문법이 두 행이면 **가장 최근 차시(no 최대) 행 하나**(P1-1). 이번 새 항목은 제외.
    · 정렬 = (틀리고 미통과) DESC, seen_count ASC, drilled_at ASC, random()  (P1-2 — drilled_at 은 단조라 seen_count 가
      «마지막으로 본 순서» 를 대신한다).
    · 은퇴 항목 제외(P1-3).
    """
    if limit <= 0:
        return []
    ranked = (
        select(
            CurMemberItem.member_id.label("member_id"),
            CurMemberItem.lesson_id.label("lesson_id"),
            CurMemberItem.item_id.label("item_id"),
            func.row_number().over(
                partition_by=CurMemberItem.item_id, order_by=CurLesson.no.desc()
            ).label("rn"),
        )
        .join(CurLesson, CurLesson.lesson_id == CurMemberItem.lesson_id)
        .join(CurItem, CurItem.item_id == CurMemberItem.item_id)
        .where(
            CurMemberItem.member_id == member_id,
            CurMemberItem.drilled_at.is_not(None),
            CurItem.retired_at.is_(None),
        )
    )
    if exclude_item_ids:
        ranked = ranked.where(CurMemberItem.item_id.not_in(list(exclude_item_ids)))
    ranked = ranked.subquery("ranked")
    wrong_first = case(
        (and_(CurMemberItem.quiz_failed_count > 0, CurMemberItem.quiz_passed_at.is_(None)), 1), else_=0
    )
    stmt = (
        select(CurMemberItem, CurItem)
        .join(ranked, and_(
            ranked.c.member_id == CurMemberItem.member_id,
            ranked.c.lesson_id == CurMemberItem.lesson_id,
            ranked.c.item_id == CurMemberItem.item_id,
        ))
        .join(CurItem, CurItem.item_id == CurMemberItem.item_id)
        .where(ranked.c.rn == 1)
        .order_by(
            wrong_first.desc(),
            CurMemberItem.seen_count.asc(),
            CurMemberItem.drilled_at.asc(),
            func.random(),
        )
        .limit(limit)
    )
    return [(mi, it) for mi, it in db.execute(stmt).all()]


# ── 회원 × 차시 · 통화 ────────────────────────────────────────────────────── #
def lesson_status(db: Session, member_id: int, lesson_id: int) -> Optional[CurMemberLesson]:
    return db.get(CurMemberLesson, (member_id, lesson_id))


def member_lessons(db: Session, member_id: int) -> dict[int, CurMemberLesson]:
    rows = db.execute(select(CurMemberLesson).where(CurMemberLesson.member_id == member_id)).scalars().all()
    return {r.lesson_id: r for r in rows}


def cur_call(db: Session, call_id: int) -> Optional[CurCall]:
    return db.get(CurCall, call_id)


def call_member_id(db: Session, call_id: int) -> Optional[int]:
    """통화의 회원 — cur_call 은 회원을 갖지 않는다(call 이 원본)."""
    return db.execute(select(Call.member_id).where(Call.call_id == call_id)).scalar_one_or_none()


def lessons(db: Session, language: str, level_no: Optional[int] = None) -> list[CurLesson]:
    stmt = select(CurLesson).where(CurLesson.language == language)
    if level_no is not None:
        stmt = stmt.where(CurLesson.level_no == level_no)
    return list(db.execute(stmt.order_by(CurLesson.no.asc())).scalars().all())


def drilled_count(db: Session, member_id: int, lesson_id: int) -> int:
    return int(db.execute(
        select(func.count()).select_from(CurMemberItem).where(
            CurMemberItem.member_id == member_id,
            CurMemberItem.lesson_id == lesson_id,
            CurMemberItem.drilled_at.is_not(None),
        )
    ).scalar_one() or 0)


def member_call_ids(db: Session, member_id: int) -> list[int]:
    """reset 용 — 그 회원의 cur_call 행(call 조인)."""
    return list(db.execute(
        select(CurCall.call_id).join(Call, Call.call_id == CurCall.call_id).where(Call.member_id == member_id)
    ).scalars().all())
