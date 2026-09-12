"""커리큘럼 2단계(cur_*) 조회 DTO — GET /api/v1/cur/me · GET /api/v1/cur/lessons.

계획 docs/plans/2026-09-12-cur-2단계-통화경로-이전.md §2·§8. 값은 전부 `curriculum_service.me / lessons` 가 만든 dict 를 그대로
검증한다(라우터는 얇다). 필드를 늘리면 여기와 서비스 dict 를 같이 고친다 — 시험(tests/test_cur_router.py)이 키를 잠근다.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

#: cur_member_lesson.status 값(curriculum_service.STATUS_*). 안 시작한 차시는 None(목록에서만).
CurStatus = Literal["learning", "expression_done", "freetalk_done"]


class CurLessonOut(BaseModel):
    """현재 차시 — 앱 홈 카드가 그린다."""

    no: int
    #: 예 `L1-S01-1`(레벨-상황-순번). 하네스·로그가 이걸로 차시를 식별한다.
    code: str
    level_no: int
    #: 상황 한 줄(프리토킹 «[이번 차시]» 블록의 «상황» 과 같은 값).
    situation: Optional[str] = None
    #: 주제(cur_topic.name). 없으면 None.
    topic: Optional[str] = None


class CurOpenOut(BaseModel):
    """지금 열 수 있는 코스. expression 은 언제나 True(복습 통화도 표현학습이다 — B1 결정 ②)."""

    expression: bool
    #: 그 차시 표현학습이 끝났고(expression_done) 아직 프리토킹을 안 했을 때만 True. False 인데 freetalk 을 열면 WS 가 COURSE_LOCKED.
    freetalk: bool


class CurMeOut(BaseModel):
    """GET /cur/me — 내 커리큘럼 위치 한 장."""

    lesson: CurLessonOut
    status: CurStatus
    #: 이 차시 항목 수(퇴출분 제외).
    items_total: int
    #: 그중 이 회원이 드릴까지 간 항목 수(cur_member_item.drilled_at). 진행률 = items_drilled / items_total.
    items_drilled: int
    open: CurOpenOut
    #: auto 로 통화를 걸면 서버가 정할 코스(홈 «이번 통화» 카드). expression_done 이면 freetalk, 아니면 expression.
    next_course: Literal["expression", "freetalk"]


class CurLessonRowOut(BaseModel):
    """GET /cur/lessons 한 줄 — 차시 + 내 상태(안 시작한 차시는 status None)."""

    no: int
    code: str
    level_no: int
    situation: Optional[str] = None
    status: Optional[CurStatus] = None
