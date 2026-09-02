"""제출 배선 — 학습자의 산출을 과제 id 로 묶는다.

`submission` 행은 과제 생성 시 전원분이 `not_started` 로 깔린다. 이 모듈이 그걸 채운다.

두 활동의 성격이 다르다.

| 활동 | 서버가 이미 아는가 | 방법 |
|---|---|---|
| **회화** | **안다.** `item_evidence` 가 통화별로 쌓인다 | 서버 단독 자동 배선(`link_call`) |
| **발음** | **모른다.** `learning_item` 예문을 채점하는 서버 경로가 없다 | 앱이 보고해야 함(`record_speaking`) |

★ 수행 판정은 **시간도 턴 수도 아니다 — 증거 유무다.**
  D15 로 `call.is_valid_call` 이 폐지됐다. 30초 통화라도 목표 항목이 하나 잡히면 수행이고,
  5분을 채웠어도 0건이면 미수행이다. **출석이 아니라 산출을 센다**(`06_회화설계.md` §6.1).

⛔ 여기에 시간 임계를 새로 만들지 마라. 그 규칙은 한 번 폐지된 것이다.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from domains.classroom.models.assignment import Assignment
from domains.classroom.models.classroom_member import ClassroomMember
from domains.classroom.models.submission import Submission
from domains.classroom.service.conversation_goal import conversation_target_ids
from domains.learning.models.item_evidence import ItemEvidence
from domains.learning.models.learning_item import LearningItem

# 「썼다」로 세는 등급. E1(모방)은 세지 않는다 — 비버가 방금 한 말을 따라한 것은
# 사용이 아니다(`06` §4). E0·F 도 당연히 제외.
USED_GRADES = frozenset({"E2", "E3"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _target_ids(assignment: Assignment) -> set[int]:
    try:
        return set(json.loads(assignment.target_item_ids or "[]"))
    except (TypeError, ValueError):
        return set()


def _activities(assignment: Assignment) -> set[str]:
    try:
        return set(json.loads(assignment.activities or "[]"))
    except (TypeError, ValueError):
        return set()


def open_assignments_for(db: Session, member_id: int, activity: str) -> list[tuple[Assignment, ClassroomMember]]:
    """이 회원이 지금 수행 중일 수 있는 과제 — 활동 종류로 거른다.

    - 닫힌 과제(`closed_at`)는 제외한다. 마감 후 통화가 소급 반영되면 교사가 이미 본
      결과가 뒤에서 바뀐다.
    - 이탈한 명단 행은 제외한다. 익명화되면 `member_id` 가 NULL 이라 어차피 안 잡힌다.
    - **마감(`due_at`) 은 거르지 않는다.** 지각 제출도 제출이다. 늦었는지는 교사가
      `completed_at` 으로 판단한다.
    """
    stmt = (
        select(Assignment, ClassroomMember)
        .join(ClassroomMember, ClassroomMember.classroom_id == Assignment.classroom_id)
        .where(
            ClassroomMember.member_id == member_id,
            ClassroomMember.left_at.is_(None),
            Assignment.closed_at.is_(None),
        )
    )
    rows = list(db.execute(stmt).all())
    return [(a, cm) for a, cm in rows if activity in _activities(a)]


def _submission_for(db: Session, assignment_id: int, cm_id: int) -> Optional[Submission]:
    return db.scalar(
        select(Submission).where(
            Submission.assignment_id == assignment_id,
            Submission.classroom_member_id == cm_id,
        )
    )


def used_item_ids(db: Session, call_id: int, member_id: int) -> set[int]:
    """이 통화에서 **실제로 쓴** 항목 id — E2·E3 만."""
    stmt = select(ItemEvidence.item_id, ItemEvidence.grade_final).where(
        ItemEvidence.call_id == call_id,
        ItemEvidence.member_id == member_id,
        ItemEvidence.verified.is_(True),
    )
    return {item_id for item_id, grade in db.execute(stmt).all() if grade in USED_GRADES}


def conversation_goal_ids(db: Session, assignment: Assignment) -> set[int]:
    """이 과제의 **회화 목표** 항목 id.

    ⛔ 과제의 목표 문장 전체(최대 40)가 아니다. 회화는 통화 한 번에 40개를 다 쓰라는
       뜻이 아니므로 우선순위 상위 N개만 목표로 센다(`conversation_goal.py`).

    ★ 과제 생성 때 이미 같은 정의로 `conversation_total` 을 넣어 두지만, 여기서 다시
      계산한다 — 과제 생성 후에 참여한 학습자는 제출 행이 없어 그 값을 못 물려받는다.
    """
    ids = _target_ids(assignment)
    if not ids:
        return set()
    rows = db.scalars(select(LearningItem).where(LearningItem.item_id.in_(ids))).all()
    return set(conversation_target_ids(rows))


def link_call(db: Session, member_id: int, call_id: int) -> list[dict]:
    """통화 1건을 진행 중인 회화 과제들에 묶는다. **커밋하지 않는다.**

    호출자(`_apply_call_mastery`)가 증거 적립·승급 판정과 **한 커밋**으로 묶는다.
    부분 커밋 창을 만들지 않기 위해서다.

    ★ 한 통화가 **여러 과제**를 동시에 만족시킬 수 있다. 반 두 곳에 속한 학습자가
      그렇다. 통화 1건을 과제 1건에 귀속시킬 근거가 없으므로 전부 갱신한다.
    """
    used = used_item_ids(db, call_id, member_id)
    if not used:
        return []  # 증거가 없으면 수행이 아니다. 아무것도 건드리지 않는다.

    linked: list[dict] = []
    for assignment, cm in open_assignments_for(db, member_id, "conversation"):
        targets = _target_ids(assignment)
        if not (used & targets):
            continue  # 이 과제의 문장을 하나도 안 썼다 — 이 통화는 이 과제와 무관하다

        # ★ 귀속은 목표 문장 전체로 판단하고, **점수는 회화 목표(상위 N)로만** 센다.
        #   좁혀서 귀속까지 막으면 실제로 한 통화가 「안 했다」로 남는다.
        goal = conversation_goal_ids(db, assignment)
        met = used & goal

        sub = _submission_for(db, assignment.assignment_id, cm.classroom_member_id)
        if sub is None:
            # 과제 생성 후 참여한 학습자 — 행이 없을 수 있다. 만들어 준다.
            sub = Submission(
                assignment_id=assignment.assignment_id,
                classroom_member_id=cm.classroom_member_id,
                status="not_started",
            )
            db.add(sub)

        sub.conversation_met = len(met)
        # ⛔ `len(targets)`(=40) 를 쓰던 자리다. 과제 생성 때 넣은 값과 정의가 갈려
        #    첫 통화 뒤에 분모가 11 에서 40 으로 바뀌어 있었다.
        sub.conversation_total = len(goal)
        sub.call_id = call_id
        sub.status = "done"
        sub.completed_at = sub.completed_at or _now()
        linked.append(
            {
                "assignment_id": assignment.assignment_id,
                "met": len(met),
                "of": len(goal),
            }
        )
    return linked


def record_speaking(
    db: Session,
    assignment: Assignment,
    cm: ClassroomMember,
    *,
    passed: int,
    total: int,
    failed_item_ids: Iterable[int],
) -> Submission:
    """발음 과제 결과를 기록한다. **앱이 호출해야 한다 — 서버가 만들 수 없다.**

    `speaking_passed` 는 **점수가 아니라 AI 가 알아들은 문장 수**다.
    `failed_item_ids` 가 `weak_items()`(다시 가르칠 문장)의 유일한 재료다.

    ⛔ 발음 챌린지(`pronunciation_challenge`)는 서버에 결과를 보내지 않는다.
       여기 들어오는 것은 `/learning/intro` 계열의 과제 수행분이다(`10` §7.2·§7.3).
    """
    sub = _submission_for(db, assignment.assignment_id, cm.classroom_member_id)
    if sub is None:
        sub = Submission(
            assignment_id=assignment.assignment_id,
            classroom_member_id=cm.classroom_member_id,
            status="not_started",
        )
        db.add(sub)

    sub.speaking_passed = passed
    sub.speaking_total = total
    sub.failed_item_ids = json.dumps(sorted(set(failed_item_ids)))
    sub.status = "done"
    sub.completed_at = sub.completed_at or _now()
    db.commit()
    db.refresh(sub)
    return sub
