"""교사 콘솔 서비스 — 반·명단·과제·결과.

집계를 새로 발명하지 않는다. 발음·회화의 증거는 이미
`member_item_progress`(체크판)와 append-only `item_evidence` 에 있다.
여기서는 **과제 id 로 묶고 반 단위로 접기만** 한다(`06_회화설계.md` §4).
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Optional, Sequence

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domains.account.models.member import Member
from domains.classroom.models.assignment import Assignment
from domains.classroom.models.classroom import Classroom
from domains.classroom.models.classroom_member import ClassroomMember
from domains.classroom.models.submission import Submission
from domains.classroom.schemas.classroom import (
    AssignmentCreate,
    ClassroomCreate,
    ClassroomUpdate,
    JoinIn,
    RosterMemberUpdate,
)
from domains.classroom.service.conversation_goal import conversation_target_ids
from domains.learning.models.call import Call
from domains.learning.models.learning_item import LearningItem

# 손글씨로 옮겨 적을 때 서로 오인되는 글자를 뺀다: I·O·0·1 (05 §3).
JOIN_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
JOIN_CODE_LEN = 6
CHAPTER_SIZE = 40

# 커리큘럼 언어축.
#
# ★ `learning_item` 은 다국어다(`language` ISO 639-1 — 유일성·FK·인덱스가 전부 언어 프리픽스).
#   B2B 과제는 **TOPIK 급수**로 챕터를 자르므로 정의상 한국어다. 이 필터를 빼면
#   챕터 40개 창에 다른 언어 어휘가 섞여 들어온다(리베이스 때 실제로 드러난 결함).
CURRICULUM_LANGUAGE = "ko"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """naive 를 UTC 로 본다.

    Postgres 는 timestamptz 를 aware 로 주지만 테스트의 sqlite 는 naive 로 준다.
    비교 전에 한 번 거쳐야 `can't compare offset-naive and offset-aware` 를 안 만난다
    (`mastery_service` 와 같은 규약).
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def generate_join_code(db: Session, *, attempts: int = 20) -> str:
    """충돌하지 않는 참여코드를 뽑는다. 유니크 제약이 최종 방어선이다."""
    for _ in range(attempts):
        code = "".join(secrets.choice(JOIN_CODE_ALPHABET) for _ in range(JOIN_CODE_LEN))
        exists = db.scalar(select(Classroom.classroom_id).where(Classroom.join_code == code))
        if not exists:
            return code
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="참여코드를 발급하지 못했습니다. 잠시 후 다시 시도해 주세요.",
    )



def vocab_example(item: LearningItem) -> Optional[str]:
    """어휘 항목의 예문 1개.

    🔴 `learning_item.examples` 는 **`kind` 에 따라 권리가 갈린다**(`07_데이터출처.md`).

    | kind | examples 의 내용 | 표시 |
    |---|---|---|
    | `vocab` | 자체 LLM 생성 문장(CEFR_문장_통합.xlsx) | 가능 |
    | `grammar` | **서울대 한국어 교재 예문** | 🔴 금지 |

    컬럼 주석은 "교재 예문"이라고만 적혀 있어 오인하기 쉽다. 이 함수를 거치지 않고
    `examples` 를 직접 읽어 화면에 내보내지 마라.
    """
    if item.kind != "vocab":
        return None
    try:
        parsed = json.loads(item.examples or "[]")
    except (TypeError, ValueError):
        return None
    return parsed[0] if parsed else None


class ClassroomService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ── 권한 ──
    def require_teacher(self, member: Member) -> None:
        # ⛔ `member.role` 을 보지 않는다 — 그건 운영 도구 축(user|admin)이다.
        if not member.is_teacher:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="교사 콘솔 권한이 없습니다.",
            )

    def owned(self, classroom_id: int, member: Member) -> Classroom:
        """내 반이 아니면 404 로 답한다 — 403 은 존재를 알려준다."""
        room = self.db.get(Classroom, classroom_id)
        if room is None or room.teacher_member_id != member.member_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="반을 찾을 수 없습니다.")
        return room

    # ── 반 ──
    def list_classrooms(self, member: Member) -> list[Classroom]:
        stmt = (
            select(Classroom)
            .where(Classroom.teacher_member_id == member.member_id)
            .order_by(Classroom.archived_at.is_(None).desc(), Classroom.created_at.desc())
        )
        return list(self.db.scalars(stmt))

    def create_classroom(self, member: Member, data: ClassroomCreate) -> Classroom:
        room = Classroom(
            teacher_member_id=member.member_id,
            name=data.name.strip(),
            target_grade=data.target_grade,
            term=data.term,
            institution=data.institution,
            teacher_display_name=data.teacher_display_name or member.name,
            capacity=data.capacity,
            join_code=generate_join_code(self.db),
        )
        self.db.add(room)
        self.db.commit()
        self.db.refresh(room)
        return room

    def update_classroom(self, room: Classroom, data: ClassroomUpdate) -> Classroom:
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(room, field, value)
        self.db.commit()
        self.db.refresh(room)
        return room

    def rotate_code(self, room: Classroom) -> Classroom:
        """새 코드 발급. 기존 코드는 즉시 무효가 되고, 이미 들어온 학습자는 남는다."""
        room.join_code = generate_join_code(self.db)
        self.db.commit()
        self.db.refresh(room)
        return room

    def archive(self, room: Classroom) -> Classroom:
        room.archived_at = _now()
        self.db.commit()
        self.db.refresh(room)
        return room

    # ── 명단 ──
    def roster(self, classroom_id: int) -> list[ClassroomMember]:
        stmt = (
            select(ClassroomMember)
            .where(
                ClassroomMember.classroom_id == classroom_id,
                ClassroomMember.left_at.is_(None),
            )
            .order_by(ClassroomMember.joined_at)
        )
        return list(self.db.scalars(stmt))

    def get_roster_member(self, classroom_id: int, cm_id: int) -> ClassroomMember:
        cm = self.db.get(ClassroomMember, cm_id)
        if cm is None or cm.classroom_id != classroom_id or cm.left_at is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="학습자를 찾을 수 없습니다.")
        return cm

    def update_roster_member(
        self, cm: ClassroomMember, data: RosterMemberUpdate
    ) -> ClassroomMember:
        payload = data.model_dump(exclude_unset=True)
        confirmed = payload.pop("confirmed", None)
        for field, value in payload.items():
            setattr(cm, field, value)
        if confirmed is not None:
            cm.confirmed_at = _now() if confirmed else None
        self.db.commit()
        self.db.refresh(cm)
        return cm

    def remove_from_class(self, cm: ClassroomMember) -> None:
        """**익명화**. 개인 식별 정보는 파기하고 집계용 행만 남긴다.

        개인정보처리방침 §3.4 ⑤ 가 두 가지를 동시에 약속한다 —
        「명단 정보와 개인별 수행 기록을 지체 없이 파기」 그리고
        「반 단위 통계값(반 평균·수행률)은 그대로 유지」.

        행을 지우면 `assignment_stats` 의 분모가 소급 변동해 두 번째 약속이 깨진다.
        그래서 **행은 남기고 사람으로 되짚을 통로를 끊는다.**

        🔴 `member_id` 를 비우는 것이 핵심이다. 나머지는 표시용이고, 이 컬럼만이
           반 기록에서 실제 이용자로 가는 링크다. 비운 뒤에는 되짚을 방법이 없다.

        재참여는 `join()` 이 `member_id` 로 기존 행을 찾으므로 익명화된 행에
        걸리지 않는다 — **새 행 = 새 동의**다. 의도한 동작이다.
        """
        cm.left_at = _now()
        cm.member_id = None
        cm.roster_name = None
        cm.student_no = None
        cm.teacher_alias = None
        self.db.commit()

    # ── 학습자 참여 ──
    def preview_by_code(self, join_code: str) -> Classroom:
        room = self.db.scalar(
            select(Classroom).where(Classroom.join_code == join_code.upper())
        )
        if room is None or room.archived_at is not None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="참여코드를 확인해 주세요."
            )
        if room.code_expires_on and room.code_expires_on < _now().date():
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="만료된 참여코드입니다.")
        return room

    def join(self, member: Member, data: JoinIn) -> ClassroomMember:
        if not data.share_consent:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="공유 동의 없이는 반에 참여할 수 없습니다.",
            )
        room = self.preview_by_code(data.join_code)

        existing = self.db.scalar(
            select(ClassroomMember).where(
                ClassroomMember.classroom_id == room.classroom_id,
                ClassroomMember.member_id == member.member_id,
            )
        )
        if existing is not None and existing.left_at is None:
            return existing

        if self.learner_count(room.classroom_id) >= room.capacity:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="반 정원이 찼습니다."
            )

        if existing is not None:  # 재참여 — 행을 되살린다
            existing.left_at = None
            existing.roster_name = data.roster_name.strip()
            existing.student_no = data.student_no
            self.db.commit()
            self.db.refresh(existing)
            return existing

        cm = ClassroomMember(
            classroom_id=room.classroom_id,
            member_id=member.member_id,
            roster_name=data.roster_name.strip(),
            student_no=data.student_no,
        )
        self.db.add(cm)
        self.db.commit()
        self.db.refresh(cm)
        return cm

    def learner_count(self, classroom_id: int) -> int:
        return int(
            self.db.scalar(
                select(func.count())
                .select_from(ClassroomMember)
                .where(
                    ClassroomMember.classroom_id == classroom_id,
                    ClassroomMember.left_at.is_(None),
                )
            )
            or 0
        )

    # ── 과제 ──
    def chapter_items(self, grade: int, chapter: int) -> list[LearningItem]:
        """챕터 = 급수 안에서 seq_no 순 40개 고정 창(`03_과제체계.md` §1)."""
        offset = (chapter - 1) * CHAPTER_SIZE
        stmt = (
            select(LearningItem)
            .where(
                LearningItem.language == CURRICULUM_LANGUAGE,
                LearningItem.kind == "vocab",
                LearningItem.topik_grade == grade,
            )
            .order_by(LearningItem.seq_no)
            .offset(offset)
            .limit(CHAPTER_SIZE)
        )
        return list(self.db.scalars(stmt))

    def chapter_range(self, grade: int, chapter: int) -> str:
        """`그림 ~ 남편` — 챕터의 어휘 범위. 화면 제목의 일부다.

        학습 대상이므로 **어느 로케일에서도 한국어로 내려간다**(`10` §12.2).
        클라이언트가 커리큘럼을 다시 들고 있을 필요가 없도록 서버가 만들어 준다.
        """
        items = self.chapter_items(grade, chapter)
        return f"{items[0].surface} ~ {items[-1].surface}" if items else ""

    def create_assignment(self, room: Classroom, data: AssignmentCreate) -> Assignment:
        if room.archived_at is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="보관된 반에는 과제를 낼 수 없습니다."
            )
        items = self.chapter_items(data.grade, data.chapter)
        if not items:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="해당 챕터에 학습 항목이 없습니다."
            )
        excluded = set(data.excluded_item_ids)
        targets = [i for i in items if i.item_id not in excluded]
        target_ids = [i.item_id for i in targets]
        if not target_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="문장을 모두 제외할 수는 없습니다."
            )
        # 회화 목표는 **뺀 것을 빼고** 센다 — 교사가 뺀 문장을 시키면 안 된다.
        conversation_total = len(conversation_target_ids(targets))

        assignment = Assignment(
            classroom_id=room.classroom_id,
            grade=data.grade,
            chapter=data.chapter,
            activities=json.dumps(list(data.activities), ensure_ascii=False),
            target_item_ids=json.dumps(target_ids),
            grammar_items=json.dumps(
                self._grammar_surfaces(data.grade, data.chapter), ensure_ascii=False
            ),
            due_at=data.due_at,
        )
        self.db.add(assignment)
        self.db.flush()

        # 명단 전원에게 미수행 행을 미리 깐다 — '누가 안 했나'가 이 제품의 값어치다.
        for cm in self.roster(room.classroom_id):
            self.db.add(
                Submission(
                    assignment_id=assignment.assignment_id,
                    classroom_member_id=cm.classroom_member_id,
                    status="not_started",
                    speaking_total=len(target_ids),
                    conversation_total=conversation_total,
                )
            )
        self.db.commit()
        self.db.refresh(assignment)
        return assignment

    def _grammar_rows(self, grade: int, chapter: int) -> list[LearningItem]:
        """문법 ↔ TOPIK 은 2:1 이다: `level_no in (2g-1, 2g)` (`07` §문법 매핑)."""
        stmt = (
            select(LearningItem)
            .where(
                LearningItem.language == CURRICULUM_LANGUAGE,
                LearningItem.kind == "grammar",
                LearningItem.level_no.in_((grade * 2 - 1, grade * 2)),
            )
            .order_by(LearningItem.seq_no)
        )
        rows = list(self.db.scalars(stmt))
        if not rows:
            return []
        per = max(1, len(rows) // max(1, self._chapter_count(grade)))
        start = (chapter - 1) * per
        return rows[start : start + per][:4]

    def _grammar_surfaces(self, grade: int, chapter: int) -> list[str]:
        """과제에 저장할 문법 표제 목록."""
        return [r.surface for r in self._grammar_rows(grade, chapter)]

    def grammar_points(self, grade: int, chapter: int) -> list[dict]:
        """문법 표제 + 예문.

        예문은 **자체 LLM 생성분**이다(`CEFR_문장_통합.xlsx` 계열). 표시 가능하다.
        2026-08-28 정정 — 이 자리를 「교재 예문」으로 적어 둔 기록이 있었으나 사실이 아니다.

        🔴 `explanation`·`caution` 은 여전히 내보내지 않는다. 그쪽은 별도 확인 전이다.
        """
        out: list[dict] = []
        for r in self._grammar_rows(grade, chapter):
            try:
                ex = json.loads(r.examples or "[]")
            except (TypeError, ValueError):
                ex = []
            out.append(
                {
                    "surface": r.surface,
                    "examples": [s for s in ex if isinstance(s, str) and s.strip()][:3],
                }
            )
        return out

    def _chapter_count(self, grade: int) -> int:
        total = int(
            self.db.scalar(
                select(func.count())
                .select_from(LearningItem)
                .where(
                    LearningItem.language == CURRICULUM_LANGUAGE,
                    LearningItem.kind == "vocab",
                    LearningItem.topik_grade == grade,
                )
            )
            or 0
        )
        return max(1, -(-total // CHAPTER_SIZE))

    def list_assignments(self, classroom_id: int) -> list[Assignment]:
        stmt = (
            select(Assignment)
            .where(Assignment.classroom_id == classroom_id)
            .order_by(Assignment.due_at.desc())
        )
        return list(self.db.scalars(stmt))

    def get_assignment(self, classroom_id: int, assignment_id: int) -> Assignment:
        a = self.db.get(Assignment, assignment_id)
        if a is None or a.classroom_id != classroom_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="과제를 찾을 수 없습니다.")
        return a

    def submissions_of(self, assignment_id: int) -> list[tuple[Submission, ClassroomMember]]:
        stmt = (
            select(Submission, ClassroomMember)
            .join(
                ClassroomMember,
                Submission.classroom_member_id == ClassroomMember.classroom_member_id,
            )
            .where(Submission.assignment_id == assignment_id)
            .order_by(Submission.status, ClassroomMember.roster_name)
        )
        return [(s, cm) for s, cm in self.db.execute(stmt)]

    # ── 집계 ──
    def assignment_stats(self, assignment: Assignment) -> dict:
        rows = self.submissions_of(assignment.assignment_id)
        done = [s for s, _ in rows if s.status == "done"]
        total = len(rows)

        def avg(values: Sequence[Optional[int]]) -> Optional[float]:
            nums = [v for v in values if v is not None]
            return round(sum(nums) / len(nums), 1) if nums else None

        target_ids = json.loads(assignment.target_item_ids or "[]")
        core_total = done[0].conversation_total if done else 0
        return {
            "completed": len(done),
            "total": total,
            "avg_speaking": avg([s.speaking_passed for s in done]),
            "avg_conversation": avg([s.conversation_met for s in done]),
            "speaking_total": len(target_ids),
            "conversation_total": core_total or 0,
        }

    def classroom_overview(self, room: Classroom, *, recent_limit: int = 20) -> dict:
        """반 단위 집계 — 홈 화면 한 판을 **1콜**로 채운다.

        콘솔은 이걸 쓰기 전까지 상태 분포와 최근 활동을 **과제 수만큼** 호출해서 만들었다.
        반이 커질수록 그대로 느려진다. 추이·증감·진도는 `list_assignments` 1콜로 되므로
        여기에 넣지 않는다 — 이미 되는 것을 옮기면 계약만 늘어난다.

        🔴 `last_seen_at` 은 **null 을 그대로 내린다.** 0 이나 현재 시각으로 채우지 마라 —
           「한 번도 안 들어온 사람」과 「오늘 들어온 사람」이 구별돼야 한다. 콘솔에서 실제로
           났던 버그다(가장 손이 필요한 사람이 미접속 필터에서 빠졌다).
        """
        assignments = self.list_assignments(room.classroom_id)
        by_id = {a.assignment_id: a for a in assignments}

        rows = list(
            self.db.execute(
                select(Submission, ClassroomMember)
                .join(
                    ClassroomMember,
                    Submission.classroom_member_id == ClassroomMember.classroom_member_id,
                )
                .where(ClassroomMember.classroom_id == room.classroom_id)
            )
        )

        status_totals = {"not_started": 0, "in_progress": 0, "done": 0}
        per_assignment: dict[int, dict] = {
            a.assignment_id: {
                "assignment_id": a.assignment_id,
                "completed": 0,
                "total": 0,
                "due_at": a.due_at,
            }
            for a in assignments
        }
        learner: dict[int, dict] = {}
        now = _now()

        for sub, cm in rows:
            if sub.status in status_totals:
                status_totals[sub.status] += 1

            slot = per_assignment.get(sub.assignment_id)
            if slot is not None:
                slot["total"] += 1
                if sub.status == "done":
                    slot["completed"] += 1

            acc = learner.setdefault(
                cm.classroom_member_id,
                {
                    "classroom_member_id": cm.classroom_member_id,
                    "done": 0,
                    "missed": 0,
                    "last_seen_at": None,
                },
            )
            if sub.status == "done":
                acc["done"] += 1
            else:
                # 미수행은 **마감이 지난 것만** 센다. 아직 기한이 남은 과제를 「놓쳤다」고
                # 세면 과제를 낸 그날 전원이 미수행자로 보인다.
                due = _aware(by_id[sub.assignment_id].due_at) if sub.assignment_id in by_id else None
                if due is not None and due < now:
                    acc["missed"] += 1

        # 명단에 있으나 제출 행이 하나도 없는 학습자도 자리를 준다(0 과 부재는 다르다).
        roster = self.roster(room.classroom_id)
        for cm in roster:
            learner.setdefault(
                cm.classroom_member_id,
                {
                    "classroom_member_id": cm.classroom_member_id,
                    "done": 0,
                    "missed": 0,
                    "last_seen_at": None,
                },
            )

        # 마지막 접속 = 마지막 통화 시각. 과제 수행이 아니라 **앱 사용**이 기준이다.
        # 반 가입만 하고 앱 계정이 아직 안 붙은 명단(member_id 없음)은 null 로 남는다.
        member_ids = [cm.member_id for cm in roster if cm.member_id is not None]
        if member_ids:
            seen = dict(
                self.db.execute(
                    select(Call.member_id, func.max(Call.created_at))
                    .where(Call.member_id.in_(member_ids))
                    .group_by(Call.member_id)
                ).all()
            )
            for cm in roster:
                if cm.member_id in seen:
                    learner[cm.classroom_member_id]["last_seen_at"] = seen[cm.member_id]

        recent = sorted(
            (
                {
                    "classroom_member_id": cm.classroom_member_id,
                    "roster_name": cm.roster_name,
                    "assignment_id": sub.assignment_id,
                    "status": sub.status,
                    "completed_at": sub.completed_at,
                }
                for sub, cm in rows
                if sub.completed_at is not None
            ),
            key=lambda r: _aware(r["completed_at"]),
            reverse=True,
        )[:recent_limit]

        return {
            "assignment_count": len(assignments),
            "status_totals": status_totals,
            "per_assignment": [per_assignment[a.assignment_id] for a in assignments],
            "recent": recent,
            "learner_totals": [learner[cm.classroom_member_id] for cm in roster],
        }

    def weak_items(self, assignment: Assignment, limit: int = 3) -> list[dict]:
        """다시 가르쳐야 할 문장 — 미통과가 많은 순.

        `submission.failed_item_ids` 를 접는다. 제출이 없으면 빈 목록이다
        — 숫자를 지어내지 않는다.
        """
        counter: dict[int, int] = {}
        done = 0
        for s, _ in self.submissions_of(assignment.assignment_id):
            if s.status != "done":
                continue
            done += 1
            for item_id in json.loads(s.failed_item_ids or "[]"):
                counter[item_id] = counter.get(item_id, 0) + 1
        if not counter:
            return []
        top = sorted(counter.items(), key=lambda kv: -kv[1])[:limit]
        items = {
            i.item_id: i
            for i in self.db.scalars(
                select(LearningItem).where(
                    LearningItem.item_id.in_([k for k, _ in top])
                )
            )
        }
        out = []
        for item_id, failed in top:
            item = items.get(item_id)
            if item is None:
                continue
            out.append(
                {
                    "item_id": item_id,
                    "surface": item.surface,
                    "example": vocab_example(item),
                    "kind": item.kind,
                    "hit": done - failed,
                    "of": done,
                }
            )
        return out
