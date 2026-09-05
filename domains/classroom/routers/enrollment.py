"""학습자 참여 라우터 — 앱에서 부르는 쪽(A1~A5).

콘솔(`/console/*`)과 분리한 이유: 권한 주체가 다르다. 여기는 `is_teacher` 를 보지 않는다
— 학습자는 전부 `learner` 다.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from pydantic import BaseModel, Field

from core.deps import CurrentMember, DbSession
from domains.classroom.models.assignment import Assignment
from domains.classroom.models.classroom import Classroom
from domains.classroom.models.classroom_member import ClassroomMember
from domains.classroom.models.submission import Submission
from domains.classroom.schemas.classroom import JoinIn, JoinPreviewOut
from domains.classroom.service import submission_service
from domains.classroom.service.classroom_service import ClassroomService

router = APIRouter(prefix="/classrooms", tags=["classroom-enrollment"])


@router.get("/preview", response_model=JoinPreviewOut)
def preview(join_code: str, db: DbSession) -> JoinPreviewOut:
    """A2 반 확인 — 코드만으로 보여줄 수 있는 최소 정보.

    인증을 요구하지 않는다. 학습자가 코드를 잘못 적었는지 먼저 확인해야 한다.
    반 이름은 교사가 쓴 원문 그대로 내려간다(번역하지 않는다).
    """
    svc = ClassroomService(db)
    room = svc.preview_by_code(join_code)
    return JoinPreviewOut(
        classroom_id=room.classroom_id,
        name=room.name,
        institution=room.institution,
        teacher_display_name=room.teacher_display_name,
        target_grade=room.target_grade,
        term=room.term,
        learner_count=svc.learner_count(room.classroom_id),
        capacity=room.capacity,
    )


@router.post("/join", status_code=status.HTTP_201_CREATED)
def join(data: JoinIn, member: CurrentMember, db: DbSession) -> dict:
    """A3 이름·공유 동의 → A4 완료.

    `roster_name` 은 학습자가 직접 적는다. 앱에서 쓰는 `member.name` 과 별개다
    (`04_학습자관리.md` §2). 동의 없이는 참여시키지 않는다.
    """
    cm = ClassroomService(db).join(member, data)
    room = db.get(Classroom, cm.classroom_id)
    return {
        "classroom_member_id": cm.classroom_member_id,
        "classroom_id": cm.classroom_id,
        "classroom_name": room.name if room else "",
        "roster_name": cm.roster_name,
    }


@router.get("/my/assignments")
def my_assignments(member: CurrentMember, db: DbSession) -> list[dict]:
    """A5 숙제 목록 — 내가 속한 반들의 과제.

    화면 문안은 앱이 로케일로 조립한다. 여기서는 **데이터만** 내려보낸다
    — 서버가 문안을 만들면 30개 로케일이 서버로 넘어온다(`10` §3).
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(Assignment, Classroom, Submission)
        .join(Classroom, Assignment.classroom_id == Classroom.classroom_id)
        .join(
            ClassroomMember,
            (ClassroomMember.classroom_id == Classroom.classroom_id)
            & (ClassroomMember.member_id == member.member_id)
            & (ClassroomMember.left_at.is_(None)),
        )
        .outerjoin(
            Submission,
            (Submission.assignment_id == Assignment.assignment_id)
            & (Submission.classroom_member_id == ClassroomMember.classroom_member_id),
        )
        .order_by(Assignment.due_at.desc())
    )
    out = []
    for a, room, sub in db.execute(stmt):
        out.append(
            {
                "assignment_id": a.assignment_id,
                "classroom_name": room.name,
                "grade": a.grade,
                "chapter": a.chapter,
                "activities": json.loads(a.activities or "[]"),
                "item_ids": json.loads(a.target_item_ids or "[]"),
                "due_at": a.due_at,
                "overdue": a.due_at < now,
                "status": sub.status if sub else "not_started",
                "speaking_passed": sub.speaking_passed if sub else None,
                "speaking_total": sub.speaking_total if sub else None,
                "conversation_met": sub.conversation_met if sub else None,
                "conversation_total": sub.conversation_total if sub else None,
            }
        )
    return out


@router.delete("/{classroom_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
def leave(classroom_id: int, member: CurrentMember, db: DbSession) -> None:
    """DA1 반 나가기 = **동의 철회**.

    개인정보처리방침 §3.5 ⑤ — 철회 즉시 명단에서 사라지고 교사가 조회할 수 없다.
    반 명단 정보(반에서 쓸 이름·학번)는 파기되고, 반 단위 통계값만 남는다.

    앱 계정과 개인 학습 기록(`item_evidence`·레벨)은 지워지지 않는다.
    그건 회사의 서비스 제공이지 기관 제공분이 아니다.
    """
    cm = db.scalar(
        select(ClassroomMember).where(
            ClassroomMember.classroom_id == classroom_id,
            ClassroomMember.member_id == member.member_id,
            ClassroomMember.left_at.is_(None),
        )
    )
    if cm is not None:
        ClassroomService(db).remove_from_class(cm)


class SpeakingResultIn(BaseModel):
    """발음 과제 수행 결과.

    🔴 `passed` 는 **점수가 아니라 AI 가 알아들은 문장 수**다. 앱은 0~100 점수를
       보내지 않는다 — 교사 화면이 「12 / 14 문장」으로 읽는다(`06` §4).
    """

    passed: int = Field(ge=0, description="AI 가 알아들은 문장 수")
    total: int = Field(gt=0, description="출제 문장 수")
    failed_item_ids: list[int] = Field(default_factory=list, description="미통과 항목 id")


@router.post("/assignments/{assignment_id}/speaking", status_code=status.HTTP_200_OK)
def submit_speaking(
    assignment_id: int, data: SpeakingResultIn, member: CurrentMember, db: DbSession
) -> dict:
    """A4 발음 과제 제출.

    ⛔ **호출자가 아직 없다.** Flutter `/learning/intro` 의 완료 훅이 붙어야 동작한다.
       회화(`link_call`)와 달리 서버가 스스로 알 수 없다 — `learning_item` 예문을
       채점하는 서버 경로가 없기 때문이다(`10` §7.2).

    이 엔드포인트가 채우는 `failed_item_ids` 가 `weak_items()`(다시 가르칠 문장)의
    유일한 재료다. 여기가 비면 교사 화면의 그 칸은 영원히 빈 상태다.
    """
    assignment = db.get(Assignment, assignment_id)
    if assignment is None or assignment.closed_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="과제를 찾을 수 없습니다.")

    cm = db.scalar(
        select(ClassroomMember).where(
            ClassroomMember.classroom_id == assignment.classroom_id,
            ClassroomMember.member_id == member.member_id,
            ClassroomMember.left_at.is_(None),
        )
    )
    if cm is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="이 반의 학습자가 아닙니다.")

    if data.passed > data.total:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="통과 수가 출제 수보다 클 수 없습니다.",
        )

    sub = submission_service.record_speaking(
        db,
        assignment,
        cm,
        passed=data.passed,
        total=data.total,
        failed_item_ids=data.failed_item_ids,
    )
    return {
        "submission_id": sub.submission_id,
        "status": sub.status,
        "speaking_passed": sub.speaking_passed,
        "speaking_total": sub.speaking_total,
    }
