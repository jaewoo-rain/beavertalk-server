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
from domains.account.models.member import Member
from domains.alarm.models.alarm import Alarm
from domains.learning.service.call_service import _resolve_zone
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


def _offset_zone(offset_min: Optional[int]) -> Optional[timezone]:
    """alarm.tz_offset_min(분, 동쪽 +) → 고정 오프셋 tzinfo. 없으면 None(다음 폴백)."""
    if offset_min is None:
        return None
    return timezone(timedelta(minutes=offset_min))


def _alarm_zone(alarm: Alarm) -> ZoneInfo | timezone:
    """⭐⭐ §5(2026-09-28) — 이 알람의 시간대. **폴백은 여기(디스패치) 한 곳에만**
    둔다 — 쓰기 시점(AlarmService)에 서울을 채우면 "앱이 안 보냈다"와 "진짜
    서울이다"가 구분이 안 된다.

    순서: tz(IANA, call_service._resolve_zone 재사용 — 실패 시 경고 로그)
        → tz_offset_min(고정 오프셋) → Asia/Seoul.
    ⚠ _resolve_zone 자신의 마지막 폴백은 (그 함수를 쓰는 달력·일일한도·WS 쪽에서)
      UTC 이지만, **알람만 서울이다** — 기존 17행이 전부 서울 가정으로 만들어졌고
      tz/tz_offset_min 이 둘 다 NULL 인 알람(전부 NULL 백필)이 지금과 똑같이
      동작해야 하기 때문이다. 그래서 _resolve_zone 은 "IANA 파싱"만 재사용하고
      그 함수의 폴백 로직 자체는 쓰지 않는다.
    """
    return _resolve_zone(alarm.tz) or _offset_zone(alarm.tz_offset_min) or APP_TZ


class DispatchService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def run(self) -> int:
        """디스패치 1회 실행 → 실제 링한 회원 수(발송 성공 토큰 수)를 반환.

        ⭐⭐ §5(2026-09-28) — 알람마다 **자기 시간대**의 시·분·요일을 같이 본다.
        옛 버그: 후보 시각을 서울 벽시각으로만 만들고 시·분만 그 알람 기준으로
        대조했다 — 요일은 여전히 서울 것이었다. 뉴욕 사용자가 고른 「월 08:00」은
        서울 월요일(=뉴욕 일요일 19:00)에 울렸다. 이제 절대 시각 후보(`candidates`)
        를 만든 뒤, 알람마다 `_alarm_zone` 으로 변환해 시·분·요일·버킷 키를 전부
        **그 알람의 로컬 벽시계**로 계산한다.

        ⛔⛔ 멱등 키(`bucket_key`)도 **알람 시간대의 로컬 벽분**이다 — UTC 로 통일
        하면 배포 경계의 catchup 창에서 같은 알람이 다른 키로 한 번 더 울린다.
        tz·tz_offset_min 이 둘 다 NULL(기존 17행)인 알람은 `_alarm_zone` 이 서울로
        폴백하므로 이 버킷 키가 **옛 코드와 바이트 동일**하다(배포 직후 같은 분
        이중 발송 없음 — 회귀로 고정).
        """
        now = datetime.now(APP_TZ)  # 절대 시각(aware) — 라벨(APP_TZ)은 각 알람 변환 전 임시값일 뿐
        catchup = max(0, settings.INTERNAL_DISPATCH_CATCHUP_MIN)
        candidates = [
            (now - timedelta(minutes=i)).replace(second=0, microsecond=0)
            for i in range(catchup + 1)
        ]
        # ⛔⛔ S3(2026-09-26, Play 심사 대비) — 발송 대상 선별은 이 SELECT **하나뿐**이고
        #   run() 의 유일한 호출부는 POST /internal/dispatch-calls(외부 크론) 다. 탈퇴
        #   (Member.deleted_at)는 안 보고 있어서 탈퇴 회원의 알람이 그대로 발송됐다 —
        #   가드를 여기 한 곳에 두면 호출부가 하나뿐이라 전부 막힌다(ponytail 원칙).
        alarms = (
            self.db.execute(
                select(Alarm)
                .join(Member, Member.member_id == Alarm.member_id)
                .options(
                    selectinload(Alarm.schedules),
                    joinedload(Alarm.character),
                )
                .where(Alarm.is_activate.is_(True), Member.deleted_at.is_(None))
            )
            .scalars()
            .all()
        )
        sent = 0
        for a in alarms:
            if a.time is None:
                continue
            # ⛔⛔ §25-①(2026-09-28, 출시 전 권장) — 알람 1건 처리 실패가 나머지를
            #   막으면 안 된다. API 레벨에 tz_offset_min 범위 검사(AlarmCreate/Update,
            #   -840~840)가 생겼지만, 그 전에 API 를 직접 불러 넣은 값이나 앞으로
            #   생길 다른 실패 모드까지 여기서 다시 방어한다 — _alarm_zone 이 죽으면
            #   (예: |tz_offset_min|≥1440 → datetime.timezone ValueError) 예외가
            #   여기서 안 잡히면 이 알람 **뒤 순번의 모든 알람**이 그 분(그리고
            #   범위 밖 값이 안 고쳐지는 한 매분) 통째로 발송 안 된다(500).
            try:
                h, m = _wall_hm(a.time)
                zone = _alarm_zone(a)
                # ⛔⛔ 봄 DST 건너뜀(그 존의 로컬 02:30 같은 시각이 그날 존재하지 않음)은
                #   **수용한 결정**이다(§5, 2026-09-28) — 그날은 조용히 안 울린다.
                #   ⛔ "가까운 유효 시각으로 밀어서라도 울리게" 고치지 마라 — 사용자가
                #   예상 못 한 시각에 전화를 받는 게 더 나쁘다. 1년에 한 번, DST 지역
                #   에서만 벌어진다. (가을 되돌림은 같은 로컬시각이 두 번 오는데, 멱등
                #   키가 로컬 벽분이라 두 번째 instant 가 같은 키로 막혀 자연히 1회만
                #   울린다 — 이쪽은 이미 바람직하게 동작한다.)
                for cand in candidates:
                    b = cand.astimezone(zone)
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
            except Exception:  # noqa: BLE001 - 알람 1건의 실패가 나머지 알람을 막으면 안 된다(R5)
                logger.exception(
                    "dispatch: 알람 처리 실패(건너뜀, 나머지는 계속) alarm_id=%s tz=%r tz_offset_min=%s",
                    a.alarm_id, a.tz, a.tz_offset_min,
                )
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
