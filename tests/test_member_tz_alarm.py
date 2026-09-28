"""알람 시간대 자동 추적(2026-09-29) — member.tz 기록 + 디스패치 존 우선순위 + 이중 발송 가드.

문서: docs/20260929_0005_알람시간대-자동추적-member-tz.md

검증:
  A. remember_device_tz — 유효 IANA 만 저장(정규 이름) / 잘못된 값은 경고 + 저장 안 함 /
     값이 같으면 쓰기 없음 / 저장 실패가 예외로 새지 않음.
  B. 조회 API(daily-status·stats/calendar) — tz 를 받으면 member.tz 갱신, 저장이 실패해도 응답 정상.
  C. 디스패치 — member.tz 가 alarm.tz 를 이긴다 / 둘 다 없으면 서울(기존 11개와 동일한 키).
  D. 시간대 변경 순간 이중 발송 없음 — 같은 날짜 키는 UNIQUE 가, 날짜변경선의 다른 날짜
     키는 _REFIRE_GUARD 가 막는다. 알람 시각을 고친 경우(시·분이 다른 키)는 막지 않는다.

외부 의존 0(인메모리 sqlite). 디스패치 하네스는 tests/test_push_dispatch.py 를 재사용하되,
_claim 은 **실제로 로그 행을 쓰는** 가짜로 바꿔 UNIQUE 의미를 흉내 낸다(pg ON CONFLICT 는
sqlite 에서 안 돈다).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import core.fcm as fcm_mod
from core.fcm import FcmSendResult
from domains.account.models.member import Member
from domains.alarm.models.alarm import Alarm
from domains.learning.routers.call import get_daily_status
from domains.learning.routers.stats import get_calendar
from domains.learning.service import call_service
from domains.push.models.push_dispatch_log import PushDispatchLog
from domains.push.service.dispatch_service import DispatchService, _DAY_CODES
from tests.test_push_dispatch import (  # noqa: F401 - fixture 재사용
    APP_TZ,
    _patch_now,
    _seed,
    _sentinel_time,
    session_factory,
)


# --------------------------------------------------------------------------- #
# 하네스 — 로그 행을 실제로 쓰는 _claim(UNIQUE 흉내)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def real_claims(monkeypatch):
    """_claim 이 PushDispatchLog 행을 쓴다 — 같은 (alarm, key)면 None(=UNIQUE 충돌)."""
    claims: list[tuple[int, str]] = []
    clock = SimpleNamespace(now=None)  # 로그 created_at(실제 DB 에선 now())

    def fake_claim(self, alarm_id, bucket_key):
        dup = self.db.execute(
            select(PushDispatchLog).where(
                PushDispatchLog.alarm_id == alarm_id,
                PushDispatchLog.intended_fire_minute == bucket_key,
            )
        ).first()
        if dup is not None:
            return None
        cid = f"call-{alarm_id}-{bucket_key}"
        self.db.add(PushDispatchLog(
            alarm_id=alarm_id, intended_fire_minute=bucket_key, call_id=cid,
            created_at=clock.now.astimezone(timezone.utc),
        ))
        self.db.commit()
        claims.append((alarm_id, bucket_key))
        return cid

    monkeypatch.setattr(DispatchService, "_claim", fake_claim)
    monkeypatch.setattr(DispatchService, "_purge", lambda self: None)
    monkeypatch.setattr(
        fcm_mod, "send_incoming_call", lambda **kw: FcmSendResult(sent=1, dead_tokens=[]),
    )
    return SimpleNamespace(claims=claims, clock=clock)


def _run_at(monkeypatch, db, real_claims, instant: datetime) -> int:
    _patch_now(monkeypatch, instant.astimezone(APP_TZ))
    real_claims.clock.now = instant
    return DispatchService(db).run()


def _set_member_tz(db, member_id: int, tz: str | None) -> None:
    db.get(Member, member_id).tz = tz
    db.commit()


# =========================================================================== #
# A. remember_device_tz
# =========================================================================== #
def test_remember_saves_valid_iana_tz(session_factory):
    db = session_factory()
    ids = _seed(db, alarm_time=None, is_activate=False, day_codes=set(), tokens=[])
    call_service.remember_device_tz(db, ids["member_id"], "America/New_York")
    assert db.get(Member, ids["member_id"]).tz == "America/New_York"


@pytest.mark.parametrize("bad", ["Not/AZone", "asdf", "../../etc/passwd", "/abs/path", "x" * 200])
def test_remember_rejects_invalid_tz_with_warning(session_factory, caplog, bad):
    """⛔ 쓰레기를 저장하면 알람이 엉뚱해진다 — 경고만 남기고 기존 값을 지킨다."""
    db = session_factory()
    ids = _seed(db, alarm_time=None, is_activate=False, day_codes=set(), tokens=[])
    _set_member_tz(db, ids["member_id"], "Asia/Tokyo")
    with caplog.at_level(logging.WARNING):
        call_service.remember_device_tz(db, ids["member_id"], bad)
    assert db.get(Member, ids["member_id"]).tz == "Asia/Tokyo"
    assert any("저장 안 함" in r.getMessage() for r in caplog.records)


def test_remember_none_or_empty_is_noop(session_factory):
    db = session_factory()
    ids = _seed(db, alarm_time=None, is_activate=False, day_codes=set(), tokens=[])
    call_service.remember_device_tz(db, ids["member_id"], None)
    call_service.remember_device_tz(db, ids["member_id"], "")
    assert db.get(Member, ids["member_id"]).tz is None


def test_remember_same_value_does_not_write(session_factory, monkeypatch):
    """매 호출 쓰기 금지 — 값이 같으면 commit 자체를 안 한다."""
    db = session_factory()
    ids = _seed(db, alarm_time=None, is_activate=False, day_codes=set(), tokens=[])
    _set_member_tz(db, ids["member_id"], "Asia/Seoul")
    commits = []
    monkeypatch.setattr(db, "commit", lambda: commits.append(1))
    call_service.remember_device_tz(db, ids["member_id"], "Asia/Seoul")
    assert commits == []


def test_remember_swallows_db_failure(session_factory, monkeypatch, caplog):
    db = session_factory()
    ids = _seed(db, alarm_time=None, is_activate=False, day_codes=set(), tokens=[])

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "commit", boom)
    with caplog.at_level(logging.WARNING):
        call_service.remember_device_tz(db, ids["member_id"], "Europe/Paris")  # 예외 없음
    assert any("저장 실패" in r.getMessage() for r in caplog.records)


# =========================================================================== #
# B. 조회 API — 갱신 + 실패해도 200
# =========================================================================== #
def test_daily_status_records_member_tz(session_factory):
    db = session_factory()
    ids = _seed(db, alarm_time=None, is_activate=False, day_codes=set(), tokens=[])
    member = db.get(Member, ids["member_id"])
    out = get_daily_status(date="2026-09-29", member=member, db=db, tz_offset=-240,
                           tz="America/New_York")
    assert out["date"] == "2026-09-29"
    assert db.get(Member, ids["member_id"]).tz == "America/New_York"


def test_calendar_records_member_tz(session_factory):
    db = session_factory()
    ids = _seed(db, alarm_time=None, is_activate=False, day_codes=set(), tokens=[])
    member = db.get(Member, ids["member_id"])
    out = get_calendar(start="2026-09-01", end="2026-09-29", member=member, db=db,
                       tz="Europe/Berlin", tz_offset_min=None)
    assert "days" in out
    assert db.get(Member, ids["member_id"]).tz == "Europe/Berlin"


def test_read_apis_still_answer_when_tz_save_fails(session_factory, monkeypatch):
    """⛔ 읽기 API 다 — tz 저장이 실패해도 조회 결과는 그대로 나가야 한다(R5)."""
    db = session_factory()
    ids = _seed(db, alarm_time=None, is_activate=False, day_codes=set(), tokens=[])
    member = db.get(Member, ids["member_id"])

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "commit", boom)
    out = get_daily_status(date="2026-09-29", member=member, db=db, tz_offset=540,
                           tz="Asia/Tokyo")
    assert out["date"] == "2026-09-29"
    cal = get_calendar(start="2026-09-01", end="2026-09-29", member=member, db=db,
                       tz="Asia/Tokyo", tz_offset_min=None)
    assert "days" in cal


def test_read_api_with_invalid_tz_answers_and_does_not_save(session_factory):
    db = session_factory()
    ids = _seed(db, alarm_time=None, is_activate=False, day_codes=set(), tokens=[])
    member = db.get(Member, ids["member_id"])
    out = get_daily_status(date="2026-09-29", member=member, db=db, tz_offset=540,
                           tz="Mars/Olympus")
    assert out["date"] == "2026-09-29"
    assert db.get(Member, ids["member_id"]).tz is None


# =========================================================================== #
# C. 디스패치 존 우선순위
# =========================================================================== #
def test_member_tz_beats_alarm_tz(session_factory, monkeypatch, real_claims):
    """⭐⭐ 뉴욕에서 만든 「월 08:00」 알람 — 회원이 지금 서울에 있으면 **서울** 08:00 에 울린다."""
    seoul_local = datetime(2026, 7, 6, 8, 0, tzinfo=APP_TZ)  # 월
    db = session_factory()
    ids = _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True,
                day_codes={_DAY_CODES[seoul_local.weekday()]}, tokens=[("t", True)],
                tz="America/New_York")
    _set_member_tz(db, ids["member_id"], "Asia/Seoul")

    assert _run_at(monkeypatch, db, real_claims, seoul_local) == 1
    assert real_claims.claims == [(ids["alarm_id"], "2026-07-06 08:00")]

    # 그리고 뉴욕 08:00(=서울 21:00)에는 더 이상 울리지 않는다(시간대가 고정되지 않는다).
    ny_local = datetime(2026, 7, 6, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    assert _run_at(monkeypatch, db, real_claims, ny_local) == 0
    db.close()


def test_member_tz_used_when_alarm_has_no_tz(session_factory, monkeypatch, real_claims):
    """기존 11개 중 10개의 모양 — alarm.tz NULL + 외국 회원. member.tz 가 채워지면 현지 시각."""
    ny_local = datetime(2026, 7, 6, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    db = session_factory()
    ids = _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True,
                day_codes={_DAY_CODES[ny_local.weekday()]}, tokens=[("t", True)])
    _set_member_tz(db, ids["member_id"], "America/New_York")
    assert _run_at(monkeypatch, db, real_claims, ny_local) == 1
    assert real_claims.claims == [(ids["alarm_id"], "2026-07-06 08:00")]
    db.close()


def test_both_null_still_seoul_byte_identical(session_factory, monkeypatch, real_claims):
    """member.tz·alarm.tz 모두 NULL(배포 직후 기존 11개) → 지금과 똑같이 서울 벽분 키."""
    now = datetime(2026, 7, 8, 8, 0, tzinfo=APP_TZ)
    db = session_factory()
    ids = _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True,
                day_codes={_DAY_CODES[now.weekday()]}, tokens=[("t", True)])
    assert db.get(Member, ids["member_id"]).tz is None
    assert _run_at(monkeypatch, db, real_claims, now) == 1
    assert real_claims.claims == [(ids["alarm_id"], "2026-07-08 08:00")]
    db.close()


def test_invalid_member_tz_falls_back_to_alarm_tz(session_factory, monkeypatch, real_claims):
    """저장 경로가 막지만, 혹시 DB 에 잘못된 값이 있으면 alarm.tz 로 폴백(경고)."""
    ny_local = datetime(2026, 7, 6, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    db = session_factory()
    ids = _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True,
                day_codes={_DAY_CODES[ny_local.weekday()]}, tokens=[("t", True)],
                tz="America/New_York")
    _set_member_tz(db, ids["member_id"], "Bad/Zone")
    assert _run_at(monkeypatch, db, real_claims, ny_local) == 1
    db.close()


# =========================================================================== #
# D. 시간대 변경 순간 이중 발송 없음
# =========================================================================== #
def test_travel_west_same_local_date_does_not_ring_twice(session_factory, monkeypatch, real_claims):
    """서울 「월 08:00」이 울린 뒤 뉴욕으로 가면, 뉴욕 「월 08:00」(13시간 뒤)은 **같은 키**라
    UNIQUE 가 막는다 — 한 로컬 날짜·시각 발생당 1회."""
    seoul_local = datetime(2026, 7, 6, 8, 0, tzinfo=APP_TZ)
    db = session_factory()
    ids = _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True,
                day_codes={"MON"}, tokens=[("t", True)])
    _set_member_tz(db, ids["member_id"], "Asia/Seoul")
    assert _run_at(monkeypatch, db, real_claims, seoul_local) == 1

    _set_member_tz(db, ids["member_id"], "America/New_York")
    ny_local = datetime(2026, 7, 6, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    assert _run_at(monkeypatch, db, real_claims, ny_local) == 0
    assert len(real_claims.claims) == 1
    db.close()


def test_dateline_tz_switch_within_catchup_does_not_ring_twice(
    session_factory, monkeypatch, real_claims,
):
    """⛔⛔ 이 과제의 함정 — 날짜변경선 양쪽(키리티마티 +14 / 호놀룰루 -10)은 **같은 순간**이
    「화 08:00」이자 「월 08:00」이다. 키가 날짜만 달라 UNIQUE 로는 안 막힌다.
    울린 직후 기기 시간대가 바뀌고 catchup 창 안에서 다시 돌면 가드가 막아야 한다."""
    monkeypatch.setattr(
        "domains.push.service.dispatch_service.settings.INTERNAL_DISPATCH_CATCHUP_MIN", 5,
    )
    kiri = ZoneInfo("Pacific/Kiritimati")
    hono = ZoneInfo("Pacific/Honolulu")
    instant = datetime(2026, 7, 7, 8, 0, tzinfo=kiri)  # 키리티마티 화 08:00
    assert instant.astimezone(hono).strftime("%a %H:%M") == "Mon 08:00"

    db = session_factory()
    ids = _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True,
                day_codes={"MON", "TUE"}, tokens=[("t", True)])
    _set_member_tz(db, ids["member_id"], "Pacific/Kiritimati")
    assert _run_at(monkeypatch, db, real_claims, instant) == 1

    _set_member_tz(db, ids["member_id"], "Pacific/Honolulu")
    assert _run_at(monkeypatch, db, real_claims, instant + timedelta(minutes=2)) == 0
    assert real_claims.claims == [(ids["alarm_id"], "2026-07-07 08:00")]
    db.close()


def test_guard_does_not_block_when_alarm_time_was_edited(session_factory, monkeypatch, real_claims):
    """가드는 **같은 시·분**만 본다 — 08:00 울린 뒤 08:05 로 고쳐 다시 울려 보는 QA 흐름은 그대로."""
    now = datetime(2026, 7, 8, 8, 0, tzinfo=APP_TZ)
    db = session_factory()
    ids = _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True,
                day_codes={_DAY_CODES[now.weekday()]}, tokens=[("t", True)])
    assert _run_at(monkeypatch, db, real_claims, now) == 1

    db.get(Alarm, ids["alarm_id"]).time = _sentinel_time(8, 5)
    db.commit()
    assert _run_at(monkeypatch, db, real_claims, now + timedelta(minutes=5)) == 1
    assert [k for _, k in real_claims.claims] == ["2026-07-08 08:00", "2026-07-08 08:05"]
    db.close()


def test_guard_does_not_block_next_day(session_factory, monkeypatch, real_claims):
    """매일 알람의 다음 날 발생(24시간 뒤)은 가드 창 밖이라 정상 발송."""
    now = datetime(2026, 7, 8, 8, 0, tzinfo=APP_TZ)
    db = session_factory()
    ids = _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True,
                day_codes=set(_DAY_CODES), tokens=[("t", True)])
    assert _run_at(monkeypatch, db, real_claims, now) == 1
    assert _run_at(monkeypatch, db, real_claims, now + timedelta(days=1)) == 1
    assert [k for _, k in real_claims.claims] == ["2026-07-08 08:00", "2026-07-09 08:00"]
    assert ids
    db.close()
