"""학습자 참여 라우터 — 앱에서 부르는 쪽(A1~A5).

콘솔(`/console/*`)과 분리한 이유: 권한 주체가 다르다. 여기는 `is_teacher` 를 보지 않는다
— 학습자는 전부 `learner` 다.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import datetime, timezone

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from sqlalchemy import select

from pydantic import BaseModel, Field

from core.deps import CurrentMember, DbSession
from domains.classroom.models.assignment import Assignment
from domains.classroom.models.classroom import Classroom
from domains.classroom.models.classroom_member import ClassroomMember
from domains.classroom.models.submission import Submission
from domains.classroom.schemas.classroom import (
    AssignmentItemOut,
    AssignmentItemsOut,
    ItemScoreOut,
    JoinIn,
    JoinPreviewOut,
)
from domains.learning.schemas.review import CharScoreOut, PronScoreOut
from domains.classroom.service import submission_service
from domains.classroom.service.classroom_service import (
    CURRICULUM_LANGUAGE,
    ClassroomService,
    vocab_example,
)
from core.speechsuper import assess_pronunciation

# 「AI 가 알아들었다」로 볼 총점 경계(0~100).
#
# 이 한 줄이 교사 화면의 「12 / 14 문장」을 정의한다. 정책이 정해지면 값만 바꾼다
# — 판정 로직을 여러 곳에 흩지 마라.
SPEAKING_PASS_SCORE = 70

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
                # 반 나가기가 id 를 요구한다. 이름만 내려보내면 앱이 나갈 대상을
                # 특정하지 못해 참여 시점의 id 를 기기에 적어 두는 우회가 생긴다.
                "classroom_id": room.classroom_id,
                "classroom_name": room.name,
                "grade": a.grade,
                "chapter": a.chapter,
                "activities": json.loads(a.activities or "[]"),
                "item_ids": json.loads(a.target_item_ids or "[]"),
                "due_at": a.due_at,
                "overdue": a.due_at < now,
                # 닫힌 과제는 제출을 받지 않는다. 앱이 이 상태를 따로 그려야 한다.
                "closed_at": a.closed_at,
                "workbook_url": a.workbook_url,
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


# ── A7 발음 과제 — 문장 목록과 무상태 채점 ──


@router.get("/assignments/{assignment_id}/items", response_model=AssignmentItemsOut)
def assignment_items(
    assignment_id: int,
    member: CurrentMember,
    db: DbSession,
    locale: str | None = None,
) -> AssignmentItemsOut:
    """A7 발음 과제가 읽을 문장 묶음.

    순서는 **출제 시점 스냅샷 그대로**다. 교사가 뺀 문장이 뒤에서 메워지면
    학습자와 교사가 다른 순서를 보게 된다.

    🔴 `example` 은 `vocab_example()` 을 반드시 거친다 — `kind='grammar'` 의
       `examples` 는 서울대 교재 예문이라 내려보내면 안 된다.

    `meaning` 은 요청 로케일(`?locale=vi`) → 영어 → None 순으로 떨어진다.
    `learning_item.meanings` 적재율이 0% 면 전부 None 이다(병합 스크립트 선행).
    """
    assignment = db.get(Assignment, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="과제를 찾을 수 없습니다.")

    svc = ClassroomService(db)
    svc.assignment_member(assignment, member)

    items = svc.assignment_items(assignment)
    return AssignmentItemsOut(
        assignment_id=assignment.assignment_id,
        grade=assignment.grade,
        chapter=assignment.chapter,
        chapter_range=svc.chapter_range(assignment.grade, assignment.chapter),
        closed=assignment.closed_at is not None,
        items=[
            AssignmentItemOut(
                item_id=i.item_id,
                seq=i.seq_no,
                surface=i.surface,
                example=vocab_example(i),
                meaning=ClassroomService.item_meaning(i, locale),
                is_core=bool(i.is_core),
            )
            for i in items
        ],
    )


@router.post(
    "/assignments/{assignment_id}/items/{item_id}/score",
    response_model=ItemScoreOut,
)
async def score_assignment_item(
    assignment_id: int,
    item_id: int,
    member: CurrentMember,
    db: DbSession,
    audio: UploadFile = File(...),
) -> ItemScoreOut:
    """과제 문장 1개의 발음 채점. **아무것도 저장하지 않는다.**

    기존 채점 경로(`POST /sentences/{id}/reviews/audio`)를 못 쓰는 이유는
    `sentence.call_id` 가 NOT NULL 이라 **통화 행부터 지어내야** 하기 때문이다.
    과제 문장은 통화에서 나온 발화가 아니다.

    통과 여부(`passed`)만 앱이 세고, 마지막에 `POST .../speaking` 으로 문장 수를
    한 번 올린다. 🔴 그쪽 `passed` 는 점수가 아니라 **알아들은 문장 수**다.
    """
    assignment = db.get(Assignment, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="과제를 찾을 수 없습니다.")
    if assignment.closed_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="마감된 과제입니다.")

    svc = ClassroomService(db)
    svc.assignment_member(assignment, member)

    item = next(
        (i for i in svc.assignment_items(assignment) if i.item_id == item_id), None
    )
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="이 과제의 문장이 아닙니다."
        )

    ref_text = vocab_example(item) or item.surface
    raw = await audio.read()
    # 채점은 무손실 원본으로 한다. 저장하지 않으므로 MP3 변환·업로드가 없다.
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
        f.write(raw)
        tmp_path = f.name
    try:
        feedback = assess_pronunciation(ref_text, tmp_path, language=CURRICULUM_LANGUAGE)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)

    raw_eval = (feedback or {}).get("evaluation") or {}
    total = raw_eval.get("total_score")
    # `PronScoreOut` 은 네 값을 모두 요구한다. 채점기가 스텁으로 떨어지면 키가
    # 빠져 응답 검증에서 500 이 난다 — 없는 값은 0 으로 채우고, 통과 판정은
    # **total 이 실제로 있었을 때만** 한다(0 으로 메운 값으로 판정하지 않는다).
    evaluation = PronScoreOut(
        total_score=int(total or 0),
        pronunciation=int(raw_eval.get("pronunciation") or 0),
        fluency=int(raw_eval.get("fluency") or 0),
        rhythm=int(raw_eval.get("rhythm") or 0),
    )
    return ItemScoreOut(
        item_id=item.item_id,
        ref_text=ref_text,
        # 「알아들었다」의 경계. 값을 바꾸려면 여기 한 줄만 고친다 —
        # 교사 화면의 「n / m 문장」이 이 선 하나로 정의된다.
        passed=bool(total is not None and int(total) >= SPEAKING_PASS_SCORE),
        evaluation=evaluation,
        char_scores=[
            CharScoreOut(
                char=str(c.get("char", "")),
                score=int(c.get("score") or 0),
                grade=str(c.get("grade", "")),
            )
            for c in ((feedback or {}).get("char_scores") or [])
            if isinstance(c, dict)
        ],
    )
