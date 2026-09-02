"""교사 콘솔 라우터 — 반·명단·과제·결과.

인증은 기존 `CurrentMember`(Supabase 토큰)를 그대로 쓴다. 교사 전용 인증 체계를
만들지 않는다(`02_기능정의.md` 절감 #1). 권한은 `member.is_teacher` 한 줄이다.

⛔ `member.role`(user|admin)은 **다른 축**이다. 그건 `/__dev` 운영 도구 접근 제어이고
   여기는 제품 권한이다. 한 컬럼에 섞으면 「관리자이면서 교사」를 표현할 수 없다.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from core.deps import CurrentMember, DbSession, GenaiClient
from domains.classroom.schemas.classroom import (
    AssignmentCreate,
    AssignmentOut,
    AssignmentResultOut,
    ClassroomCreate,
    ClassroomOut,
    ClassroomUpdate,
    RosterMemberOut,
    RosterMemberUpdate,
    SubmissionOut,
    WeakItemOut,
)
from domains.classroom.service import reminder_service
from domains.classroom.service.classroom_service import ClassroomService
from domains.classroom.service.conversation_goal import conversation_target_ids
from domains.classroom.service.summary_service import SummaryService

router = APIRouter(prefix="/console", tags=["console"])


def _classroom_out(svc: ClassroomService, room) -> ClassroomOut:
    out = ClassroomOut.model_validate(room)
    out.learner_count = svc.learner_count(room.classroom_id)
    out.assignment_count = len(svc.list_assignments(room.classroom_id))
    return out


def _assignment_out(svc: ClassroomService, a) -> AssignmentOut:
    out = AssignmentOut.model_validate(
        {
            "assignment_id": a.assignment_id,
            "classroom_id": a.classroom_id,
            "grade": a.grade,
            "chapter": a.chapter,
            "chapter_range": svc.chapter_range(a.grade, a.chapter),
            "activities": json.loads(a.activities or "[]"),
            "due_at": a.due_at,
            "closed_at": a.closed_at,
        }
    )
    for key, value in svc.assignment_stats(a).items():
        setattr(out, key, value)
    return out


# ── 반 ──
@router.get("/classrooms", response_model=list[ClassroomOut])
def list_classrooms(member: CurrentMember, db: DbSession) -> list[ClassroomOut]:
    svc = ClassroomService(db)
    svc.require_teacher(member)
    return [_classroom_out(svc, r) for r in svc.list_classrooms(member)]


@router.post("/classrooms", response_model=ClassroomOut, status_code=status.HTTP_201_CREATED)
def create_classroom(data: ClassroomCreate, member: CurrentMember, db: DbSession) -> ClassroomOut:
    svc = ClassroomService(db)
    svc.require_teacher(member)
    return _classroom_out(svc, svc.create_classroom(member, data))


@router.get("/classrooms/{classroom_id}", response_model=ClassroomOut)
def get_classroom(classroom_id: int, member: CurrentMember, db: DbSession) -> ClassroomOut:
    svc = ClassroomService(db)
    svc.require_teacher(member)
    return _classroom_out(svc, svc.owned(classroom_id, member))


@router.patch("/classrooms/{classroom_id}", response_model=ClassroomOut)
def update_classroom(
    classroom_id: int, data: ClassroomUpdate, member: CurrentMember, db: DbSession
) -> ClassroomOut:
    svc = ClassroomService(db)
    svc.require_teacher(member)
    room = svc.owned(classroom_id, member)
    return _classroom_out(svc, svc.update_classroom(room, data))


@router.post("/classrooms/{classroom_id}/join-code", response_model=ClassroomOut)
def rotate_join_code(classroom_id: int, member: CurrentMember, db: DbSession) -> ClassroomOut:
    """새 코드 발급. 기존 코드는 즉시 무효, 이미 들어온 학습자는 남는다."""
    svc = ClassroomService(db)
    svc.require_teacher(member)
    room = svc.owned(classroom_id, member)
    return _classroom_out(svc, svc.rotate_code(room))


@router.post("/classrooms/{classroom_id}/archive", response_model=ClassroomOut)
def archive_classroom(classroom_id: int, member: CurrentMember, db: DbSession) -> ClassroomOut:
    svc = ClassroomService(db)
    svc.require_teacher(member)
    room = svc.owned(classroom_id, member)
    return _classroom_out(svc, svc.archive(room))


# ── 명단 ──
@router.get("/classrooms/{classroom_id}/learners", response_model=list[RosterMemberOut])
def list_learners(classroom_id: int, member: CurrentMember, db: DbSession) -> list[RosterMemberOut]:
    svc = ClassroomService(db)
    svc.require_teacher(member)
    svc.owned(classroom_id, member)
    return [RosterMemberOut.model_validate(cm) for cm in svc.roster(classroom_id)]


@router.patch(
    "/classrooms/{classroom_id}/learners/{classroom_member_id}", response_model=RosterMemberOut
)
def update_learner(
    classroom_id: int,
    classroom_member_id: int,
    data: RosterMemberUpdate,
    member: CurrentMember,
    db: DbSession,
) -> RosterMemberOut:
    svc = ClassroomService(db)
    svc.require_teacher(member)
    svc.owned(classroom_id, member)
    cm = svc.get_roster_member(classroom_id, classroom_member_id)
    return RosterMemberOut.model_validate(svc.update_roster_member(cm, data))


@router.delete(
    "/classrooms/{classroom_id}/learners/{classroom_member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_learner(
    classroom_id: int, classroom_member_id: int, member: CurrentMember, db: DbSession
) -> None:
    """반에서 내보내기(소프트). 반 평균 집계에는 남는다."""
    svc = ClassroomService(db)
    svc.require_teacher(member)
    svc.owned(classroom_id, member)
    svc.remove_from_class(svc.get_roster_member(classroom_id, classroom_member_id))


# ── 과제 ──
# ── 반 단위 집계(홈 한 판) ──
@router.get("/classrooms/{classroom_id}/overview")
def classroom_overview(classroom_id: int, member: CurrentMember, db: DbSession) -> dict:
    """홈 화면의 상태 분포·최근 활동·학습자 누적을 한 번에 준다.

    이게 없으면 콘솔이 과제 수만큼 결과 API 를 부른다.
    """
    svc = ClassroomService(db)
    svc.require_teacher(member)
    room = svc.owned(classroom_id, member)
    return svc.classroom_overview(room)


@router.get("/classrooms/{classroom_id}/assignments", response_model=list[AssignmentOut])
def list_assignments(classroom_id: int, member: CurrentMember, db: DbSession) -> list[AssignmentOut]:
    svc = ClassroomService(db)
    svc.require_teacher(member)
    svc.owned(classroom_id, member)
    return [_assignment_out(svc, a) for a in svc.list_assignments(classroom_id)]


@router.post(
    "/classrooms/{classroom_id}/assignments",
    response_model=AssignmentOut,
    status_code=status.HTTP_201_CREATED,
)
def create_assignment(
    classroom_id: int, data: AssignmentCreate, member: CurrentMember, db: DbSession
) -> AssignmentOut:
    svc = ClassroomService(db)
    svc.require_teacher(member)
    room = svc.owned(classroom_id, member)
    return _assignment_out(svc, svc.create_assignment(room, data))


@router.get(
    "/classrooms/{classroom_id}/assignments/{assignment_id}", response_model=AssignmentResultOut
)
def assignment_result(
    classroom_id: int, assignment_id: int, member: CurrentMember, db: DbSession
) -> AssignmentResultOut:
    """과제 결과 — 누가 안 했나 · 무엇을 다시 가르쳐야 하나."""
    svc = ClassroomService(db)
    svc.require_teacher(member)
    svc.owned(classroom_id, member)
    a = svc.get_assignment(classroom_id, assignment_id)
    subs = [
        SubmissionOut.model_validate(
            {
                "classroom_member_id": s.classroom_member_id,
                "roster_name": cm.roster_name,
                "status": s.status,
                "speaking_passed": s.speaking_passed,
                "speaking_total": s.speaking_total,
                "conversation_met": s.conversation_met,
                "conversation_total": s.conversation_total,
                "completed_at": s.completed_at,
            }
        )
        for s, cm in svc.submissions_of(assignment_id)
    ]
    weak = [WeakItemOut.model_validate(w) for w in svc.weak_items(a)]
    return AssignmentResultOut(
        assignment=_assignment_out(svc, a),
        submissions=subs,
        reteach=weak,
        least_used=[],
    )


# ── 미수행 알림 보내기 ──
@router.post("/classrooms/{classroom_id}/assignments/{assignment_id}/remind")
def remind_assignment(
    classroom_id: int, assignment_id: int, member: CurrentMember, db: DbSession
):
    """아직 안 한 학습자에게 알림 1회.

    하루 1회 제한은 **서버가 건다.** 클라이언트에 두면 다른 브라우저·다른 교사가 우회한다.
    이미 보냈으면 409 와 함께 언제 보냈는지를 준다.
    """
    svc = ClassroomService(db)
    svc.require_teacher(member)
    room = svc.owned(classroom_id, member)
    assignment = svc.get_assignment(classroom_id, assignment_id)

    if not reminder_service.claim_today(db, assignment):
        db.refresh(assignment)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=jsonable_encoder(
                {
                    "detail": "already_sent_today",
                    "sent_at": assignment.manual_reminder_sent_at,
                }
            ),
        )

    db.refresh(assignment)
    return reminder_service.send_manual_reminder(db, room, assignment)


# ── 챕터 미리보기(과제 만들기 화면) ──
@router.get("/curriculum/{grade}/chapters/{chapter}")
def chapter_preview(grade: int, chapter: int, member: CurrentMember, db: DbSession) -> dict:
    """챕터의 어휘 40개 + 문법(표제 + 예문).

    예문은 어휘·문법 모두 **자체 LLM 생성분**이다. `vocab_example()` 이 어휘 쪽 권리를 가른다.
    """
    from domains.classroom.service.classroom_service import vocab_example

    svc = ClassroomService(db)
    svc.require_teacher(member)
    items = svc.chapter_items(grade, chapter)
    # ★ `core` 는 전역 `is_core` 가 아니라 **이 챕터 안의 회화 목표**다.
    #   과제 생성과 같은 헬퍼를 쓴다 — 갈리면 교사가 센 수와 실제 목표 수가 달라진다.
    core_ids = set(conversation_target_ids(items))
    return {
        "grade": grade,
        "chapter": chapter,
        "range": f"{items[0].surface} ~ {items[-1].surface}" if items else "",
        "items": [
            {
                "item_id": i.item_id,
                "seq": i.seq_no,
                "surface": i.surface,
                "example": vocab_example(i),
                "core": i.item_id in core_ids,
            }
            for i in items
        ],
        "grammar": svc.grammar_points(grade, chapter),
    }


# ── 통화 요약 (교사 로케일) ──
@router.get("/classrooms/{classroom_id}/submissions/{classroom_member_id}/summary")
def submission_summary(
    classroom_id: int,
    classroom_member_id: int,
    assignment_id: int,
    member: CurrentMember,
    db: DbSession,
    genai: GenaiClient,
    locale: str = "ko",
) -> dict:
    """회화 과제의 통화 요약을 **교사 로케일**로 준다.

    요약은 통화 시점에 학습자 로케일로 1회 생성된다. 교사 언어가 다르면 여기서
    1회 번역하고 캐시한다(`10` §12.7). 번역기가 없으면 원문 + `translated=false`.
    """
    from sqlalchemy import select

    from domains.classroom.models.submission import Submission

    svc = ClassroomService(db)
    svc.require_teacher(member)
    svc.owned(classroom_id, member)
    svc.get_roster_member(classroom_id, classroom_member_id)

    sub = db.scalar(
        select(Submission).where(
            Submission.assignment_id == assignment_id,
            Submission.classroom_member_id == classroom_member_id,
        )
    )
    if sub is None or sub.call_id is None:
        return {"text": "", "translated": False, "source_locale": None}
    return SummaryService(db, genai).get(sub.call_id, locale)
