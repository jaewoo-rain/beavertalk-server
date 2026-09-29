"""§26-④(2026-09-29) — 디스패치 건별 except 의 rollback (외부 의존 0, 인메모리 sqlite).

옛 결함: 알람 1건의 DB 오류가 공유 세션을 오염시킨 채 남아, 같은 분의 **뒤 알람 전부**가
PendingRollbackError(PG 에선 «current transaction is aborted»)로 연달아 건너뛰어졌다.

검증:
  - 알람 A 에서 DB 오류가 나도 B·C 가 **같은 분에** 발송된다.
  - A 앞에서 이미 커밋된 B 의 발송 기록(push_dispatch_log)이 rollback 으로 사라지지 않는다
    (_claim 이 INSERT 직후 그 자리에서 commit 하므로 rollback 범위는 A 의 미커밋분뿐).
  - A 의 반쪽 클레임은 남지 않는다(발송 안 한 알람이 «보냄»으로 기록되면 catchup 이 못 살린다).

세션 오염은 flush 실패로 재현한다 — sqlite 는 실패한 SELECT 로는 트랜잭션이 죽지 않지만,
flush 실패는 SQLAlchemy 세션을 «rollback 전까지 사용 불가» 상태로 만든다(PG 오염과 같은 효과).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

import core.fcm as fcm_mod
from core.fcm import FcmSendResult
from domains.push.models.push_dispatch_log import PushDispatchLog
from domains.push.service.dispatch_service import DispatchService, _DAY_CODES
from tests.test_push_dispatch import (  # noqa: F401 - fixture 재사용
    APP_TZ,
    _patch_now,
    _seed,
    _sentinel_time,
    session_factory,
)


def _setup(monkeypatch, *, poison_alarm_id_box: list):
    """_claim = 실제로 로그 행을 쓰고 commit(운영과 같은 커밋 시점). 오염 대상 알람만 flush 실패."""
    rung: list[int] = []

    def fake_claim(self, alarm_id, bucket_key):
        if alarm_id in poison_alarm_id_box:
            # NOT NULL 위반 → flush 실패 → 세션이 rollback 전까지 못 쓰는 상태가 된다.
            self.db.add(PushDispatchLog(alarm_id=alarm_id, intended_fire_minute=None))
            self.db.flush()
        cid = f"call-{alarm_id}-{bucket_key}"
        self.db.add(PushDispatchLog(alarm_id=alarm_id, intended_fire_minute=bucket_key, call_id=cid))
        self.db.commit()
        return cid

    def fake_send(**kw):
        rung.append(kw["call_id"])
        return FcmSendResult(sent=1, dead_tokens=[])

    monkeypatch.setattr(DispatchService, "_claim", fake_claim)
    monkeypatch.setattr(DispatchService, "_purge", lambda self: None)
    monkeypatch.setattr(fcm_mod, "send_incoming_call", fake_send)
    return rung


def test_db_error_on_one_alarm_does_not_skip_the_rest_and_keeps_sent_records(
    session_factory, monkeypatch,
):
    now = datetime(2026, 7, 8, 8, 0, tzinfo=APP_TZ)
    _patch_now(monkeypatch, now)
    code = _DAY_CODES[now.weekday()]
    db = session_factory()
    # 순서 = 발송 순서(sqlite 삽입순): B(정상) → A(DB 오류) → C(정상)
    b = _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True, day_codes={code}, tokens=[("tB", True)])
    a = _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True, day_codes={code}, tokens=[("tA", True)])
    c = _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True, day_codes={code}, tokens=[("tC", True)])
    rung = _setup(monkeypatch, poison_alarm_id_box=[a["alarm_id"]])

    sent = DispatchService(db).run()

    assert sent == 2, "A 의 DB 오류 뒤 C 가 같은 분에 발송되지 않았다(세션 오염)"
    assert rung == [f"call-{b['alarm_id']}-2026-07-08 08:00", f"call-{c['alarm_id']}-2026-07-08 08:00"]
    fresh = session_factory()
    logged = sorted(fresh.execute(select(PushDispatchLog.alarm_id)).scalars())
    assert logged == sorted([b["alarm_id"], c["alarm_id"]]), (
        "B 의 커밋된 발송 기록이 사라졌거나, 발송 안 한 A 가 기록됐다"
    )
    db.close()
    fresh.close()


def test_poisoned_session_without_rollback_would_skip(session_factory, monkeypatch):
    """대조군 — rollback 을 막으면 C 가 건너뛰어진다(= 이 시험이 실제로 결함을 잡는다는 증거).

    ⚠ 수정 전 코드에선 건너뛰는 정도가 아니었다 — flush 실패가 세션 객체를 만료시켜, try 밖의
    `a.time` 읽기가 PendingRollbackError 로 터지며 run() 전체가 예외(500)로 끝났다. 그래서
    그 읽기도 try 안으로 옮겼고, 여기선 «예외 없이 0건»이 기대값이다."""
    now = datetime(2026, 7, 8, 8, 0, tzinfo=APP_TZ)
    _patch_now(monkeypatch, now)
    code = _DAY_CODES[now.weekday()]
    db = session_factory()
    a = _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True, day_codes={code}, tokens=[("tA", True)])
    _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True, day_codes={code}, tokens=[("tC", True)])
    _setup(monkeypatch, poison_alarm_id_box=[a["alarm_id"]])
    monkeypatch.setattr(db, "rollback", lambda: None)

    assert DispatchService(db).run() == 0
    db.close()


def test_later_alarms_reload_correctly_after_rollback(session_factory, monkeypatch):
    """rollback 은 세션 객체를 전부 만료시킨다 — 뒤 알람은 속성을 다시 읽어 와 정상 판정한다
    (시각이 안 맞는 알람은 여전히 안 울린다)."""
    now = datetime(2026, 7, 8, 8, 0, tzinfo=APP_TZ)
    _patch_now(monkeypatch, now)
    code = _DAY_CODES[now.weekday()]
    db = session_factory()
    a = _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True, day_codes={code}, tokens=[("tA", True)])
    _seed(db, alarm_time=_sentinel_time(9, 0), is_activate=True, day_codes={code}, tokens=[("t9", True)])
    c = _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True, day_codes={code}, tokens=[("tC", True)])
    rung = _setup(monkeypatch, poison_alarm_id_box=[a["alarm_id"]])

    assert DispatchService(db).run() == 1
    assert rung == [f"call-{c['alarm_id']}-2026-07-08 08:00"]
    db.close()

