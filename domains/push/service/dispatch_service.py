"""DispatchService — 예약전화 FCM 발송 디스패처(내부 크론이 1분마다 호출).

흐름: 현재 벽분(+catchup 보정) 버킷을 만들고, 활성 알람 중 (시각·요일)이
맞는 것을 골라 (alarm, 벽분) 멱등 클레임(UNIQUE INSERT)에 성공한 건만 링한다.
멱등 로그로 중복 크론/재시도에도 이중 발송이 없다. 오래된 로그는 purge.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, joinedload, selectinload

from core import apns, fcm
from core.config import settings
from core.push_defaults import DEFAULT_CALLER_NAME
from domains.alarm.models.alarm import Alarm
from domains.push.models.device_token import DeviceToken
from domains.push.models.push_dispatch_log import PushDispatchLog

logger = logging.getLogger(__name__)
APP_TZ = ZoneInfo("Asia/Seoul")
_DAY_CODES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def _wall_hm(t: datetime) -> tuple[int, int]:
    """alarm.time(센티넬 날짜 + +00 벽시각)에서 시·분을 뽑는다."""
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc)
    return t.hour, t.minute


class DispatchService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def run(self) -> int:
        """디스패치 1회 실행 → 실제 링한 회원 수(발송 성공 토큰 수)를 반환."""
        now = datetime.now(APP_TZ)
        catchup = max(0, settings.INTERNAL_DISPATCH_CATCHUP_MIN)
        buckets = [
            (now - timedelta(minutes=i)).replace(second=0, microsecond=0)
            for i in range(catchup + 1)
        ]
        alarms = (
            self.db.execute(
                select(Alarm)
                .options(
                    selectinload(Alarm.schedules),
                    joinedload(Alarm.character),
                )
                .where(Alarm.is_activate.is_(True))
            )
            .scalars()
            .all()
        )
        sent = 0
        for a in alarms:
            if a.time is None:
                continue
            h, m = _wall_hm(a.time)
            for b in buckets:
                if b.hour != h or b.minute != m:
                    continue
                if _DAY_CODES[b.weekday()] not in {s.day_of_week for s in a.schedules}:
                    continue
                bucket_key = b.strftime("%Y-%m-%d %H:%M")
                # 클레임이 통화 id 까지 발급한다 — 발송과 기록이 갈리면 되짚기가
                # 끊긴다(캐릭터를 알람에서 못 꺼낸다).
                call_id = self._claim(a.alarm_id, bucket_key)
                if call_id is not None:
                    sent += self._ring(a, call_id)
                break
        self._purge()
        return sent

    def _claim(self, alarm_id: int, bucket_key: str) -> Optional[str]:
        """(alarm, 벽분) 멱등 클레임 + 통화 id 발급.

        Returns:
            새로 클레임했으면 발급한 call_id, 이미 발송된 버킷이면 None(발송 스킵).

        call_id 를 여기서 만드는 이유: 통화가 열릴 때 서버가 call_id → 이 로그 →
        alarm → character 로 되짚어 **캐릭터를 스스로 정한다**. 발송만 하고 기록을
        안 남기면 그 되짚기가 끊긴다.
        """
        stmt = (
            pg_insert(PushDispatchLog)
            .values(
                alarm_id=alarm_id,
                intended_fire_minute=bucket_key,
                call_id=str(uuid.uuid4()),
            )
            .on_conflict_do_nothing(
                index_elements=["alarm_id", "intended_fire_minute"]
            )
            # 충돌(중복 발송)이면 행이 없어 None — rowcount 대신 이걸로 판정한다.
            .returning(PushDispatchLog.call_id)
        )
        call_id = self.db.execute(stmt).scalar_one_or_none()
        self.db.commit()
        return call_id

    def _ring(self, alarm: Alarm, call_id: str) -> int:
        """알람 주인의 유효 토큰에 착신 푸시(android=FCM, ios=APNs VoIP). 폐기 토큰은 is_valid=False."""
        tokens = (
            self.db.execute(
                select(DeviceToken).where(
                    DeviceToken.member_id == alarm.member_id,
                    DeviceToken.is_valid.is_(True),
                )
            )
            .scalars()
            .all()
        )
        if not tokens:
            return 0
        android = [t for t in tokens if t.platform == "android_fcm"]
        ios = [t for t in tokens if t.platform == "ios_voip"]
        char = alarm.character
        name = char.name if char else DEFAULT_CALLER_NAME
        image = char.image_url if char else None
        sent = 0
        dead: list = []
        if android:
            r = fcm.send_incoming_call(
                tokens=[t.token for t in android],
                call_id=call_id,
                character_id=alarm.character_id,
                name=name,
                image_url=image,
            )
            sent += r.sent
            dead += r.dead_tokens
        if ios:
            r = apns.send_incoming_call_voip(
                tokens=[t.token for t in ios],
                call_id=call_id,
                character_id=alarm.character_id,
                name=name,
                image_url=image,
            )
            sent += r.sent
            dead += r.dead_tokens
        if dead:
            ds = set(dead)
            for t in tokens:
                if t.token in ds:
                    t.is_valid = False
            self.db.commit()
        else:
            # 폐기 토큰이 없으면 위 SELECT 로 자동 시작된 트랜잭션을 닫아준다.
            # (pgbouncer transaction 풀링에서 'idle in transaction' 커넥션 점유 방지)
            self.db.rollback()
        return sent

    def _purge(self) -> None:
        """2일 지난 멱등 로그 정리(테이블 무한 성장 방지).

        purge 는 발송보다 나중에 돌고(발송/클레임은 이미 커밋됨) 실패해도 결과에 영향이
        없으므로, 예외를 삼켜 성공한 디스패치가 500 으로 가려지지 않게 한다.
        """
        try:
            self.db.execute(
                delete(PushDispatchLog).where(
                    PushDispatchLog.created_at < func.now() - text("interval '2 days'")
                )
            )
            self.db.commit()
        except Exception as exc:  # noqa: BLE001 - purge 실패는 디스패치 성공을 가리지 않음
            logger.warning("push_dispatch_log purge 실패(무시): %s", exc)
            self.db.rollback()
