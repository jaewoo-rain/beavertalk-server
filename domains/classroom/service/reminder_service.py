"""미수행 알림 발송 — 교사가 손으로 보내는 한 번.

계약: `docs/서버작업_인수인계_2026-09-01.md` §4.2 (콘솔 저장소).

## 🔴 지금 닿지 않는 학습자가 있다 (2026-09-02 실측)

`device_token.platform` 은 프로덕션에 **`android_fcm` · `ios_voip` 둘뿐**이다.
VoIP 토큰으로는 숙제 알림을 보낼 수 없다 — iOS 는 VoIP 푸시를 받으면 즉시 CallKit 으로
착신을 보고하도록 강제하고, 안 하면 앱을 죽인다. 즉 **iOS 학습자에게는 못 보낸다.**

그래서 응답에 `skipped_unreachable_platform` 을 따로 센다. 이 사람들을
「기기 없음」으로 뭉치면 교사가 「앱을 안 깐 학생」으로 오해한다 — 앱은 깔았고
우리가 못 보내는 것이다. 앱이 일반 APNs 토큰을 등록하기 시작하면 이 칸은 0이 된다.

## 하루 1회

멱등 키 = `(assignment_id, 발송일)`. 조건부 UPDATE 로 **원자적으로** 잡는다 —
읽고 나서 쓰면 다른 브라우저·다른 교사가 그 사이를 비집는다.
`push_dispatch_log` 를 재사용하지 않는 이유는 그 테이블이 `alarm_id` NOT NULL 이라서다.

⚠ 클레임을 **발송보다 먼저** 한다(`dispatch_service._claim()` 과 같은 순서).
  발송이 통째로 실패해도 그날 칸은 소모된다. 반대로 하면 중복 발송이 생기는데,
  「두 번 울리는 것」이 「오늘 못 보내는 것」보다 나쁘다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from core import fcm
from core.push_copy import homework_reminder
from domains.account.models.member import Member
from domains.classroom.models.assignment import Assignment
from domains.classroom.models.classroom import Classroom
from domains.classroom.models.classroom_member import ClassroomMember
from domains.classroom.models.submission import Submission
from domains.push.models.device_token import DeviceToken

logger = logging.getLogger(__name__)

# 반의 시간대. 지금은 기관이 전부 국내라 앱 표준시와 같다.
# 🔴 해외 기관이 붙으면 `classroom` 에 시간대 칸을 만들고 여기를 그 값으로 바꿔라 —
#    UTC 로 자르면 교사에게 「오늘」이 안 맞는다.
CLASSROOM_TZ = ZoneInfo("Asia/Seoul")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day_start(now: datetime) -> datetime:
    """반의 시간대 기준 오늘 0시(UTC 로 환산)."""
    local = now.astimezone(CLASSROOM_TZ)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc)


def claim_today(db: Session, assignment: Assignment, *, now: Optional[datetime] = None) -> bool:
    """오늘 몫의 발송권을 잡는다. 이미 보냈으면 False."""
    now = now or _now()
    stmt = (
        update(Assignment)
        .where(
            Assignment.assignment_id == assignment.assignment_id,
            or_(
                Assignment.manual_reminder_sent_at.is_(None),
                Assignment.manual_reminder_sent_at < _day_start(now),
            ),
        )
        .values(manual_reminder_sent_at=now)
        .returning(Assignment.manual_reminder_sent_at)
        # ⛔ 동기화를 끈다. 켜 두면 SQLAlchemy 가 WHERE 절을 **파이썬에서 다시** 평가하고,
        #    그때 naive/aware 가 섞여 터진다(sqlite 가 timestamptz 를 naive 로 준다).
        #    판정은 DB 가 한다 — 호출자는 뒤에서 `refresh` 한다.
        .execution_options(synchronize_session=False)
    )
    claimed = db.execute(stmt).scalar_one_or_none()
    db.commit()
    return claimed is not None


def pending_learners(db: Session, assignment: Assignment) -> list[ClassroomMember]:
    """아직 안 한 학습자 — `status != 'done'`.

    ★ 이미 수행한 사람에게는 보내지 않는다(콘솔 D8 문안의 약속이다).
    ★ 반을 떠난 사람도 제외한다 — 남의 반 알림을 받는 셈이 된다.
    """
    stmt = (
        select(ClassroomMember)
        .join(Submission, Submission.classroom_member_id == ClassroomMember.classroom_member_id)
        .where(
            Submission.assignment_id == assignment.assignment_id,
            Submission.status != "done",
            ClassroomMember.left_at.is_(None),
        )
    )
    return list(db.scalars(stmt))


def _tokens_of(db: Session, member_id: int) -> list[DeviceToken]:
    return list(
        db.scalars(
            select(DeviceToken).where(
                DeviceToken.member_id == member_id,
                DeviceToken.is_valid.is_(True),
            )
        )
    )


def send_manual_reminder(db: Session, room: Classroom, assignment: Assignment) -> dict:
    """미수행 학습자에게 알림 1회. 발송권은 이미 잡혀 있다고 본다(`claim_today`)."""
    sent = 0
    skipped_no_device = 0
    skipped_unreachable_platform = 0
    dead: list[str] = []

    for cm in pending_learners(db, assignment):
        if cm.member_id is None:
            # 명단에만 있고 앱 계정이 아직 안 붙은 사람 — 보낼 곳이 없다.
            skipped_no_device += 1
            continue
        tokens = _tokens_of(db, cm.member_id)
        android = [t.token for t in tokens if t.platform == "android_fcm"]
        if not android:
            # ios_voip 만 있는 사람과 아무것도 없는 사람을 가른다(모듈 주석 참고).
            skipped_unreachable_platform += 1 if tokens else 0
            skipped_no_device += 0 if tokens else 1
            continue

        member = db.get(Member, cm.member_id)
        title, body = homework_reminder(
            member.language if member else None, class_name=room.name
        )
        # ★ 학습자마다 따로 보낸다. 멀티캐스트로 묶으면 어느 토큰이 성공했는지 안 돌아와
        #   「몇 명에게 갔나」를 정직하게 셀 수 없다. 반은 30명 상한이라 감당된다.
        result = fcm.send_notification(
            tokens=android,
            title=title,
            body=body,
            data={
                "type": "homework_reminder",
                "assignment_id": str(assignment.assignment_id),
                "classroom_id": str(room.classroom_id),
            },
        )
        if result.sent > 0:
            sent += 1
        else:
            # 보낼 토큰은 있었는데 전부 실패했다 — 기기가 없는 것과 다르다.
            logger.warning(
                "숙제 알림 실패 assignment_id=%s classroom_member_id=%s",
                assignment.assignment_id, cm.classroom_member_id,
            )
        dead += result.dead_tokens

    if dead:
        ds = set(dead)
        for t in db.scalars(select(DeviceToken).where(DeviceToken.token.in_(ds))):
            t.is_valid = False
        db.commit()
    else:
        # 위 SELECT 들이 연 트랜잭션을 닫는다(pgbouncer 에서 idle in transaction 방지).
        db.rollback()

    return {
        "sent": sent,
        "skipped_no_device": skipped_no_device,
        "skipped_unreachable_platform": skipped_unreachable_platform,
        "sent_at": assignment.manual_reminder_sent_at,
    }
