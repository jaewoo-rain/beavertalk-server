"""예약전화 FCM 디스패치 결정적 테스트 (외부 의존 0).

검증 대상:
    - core.fcm.send_incoming_call        : data-only 멀티캐스트 페이로드 / 폐기토큰 분류 / graceful.
    - DispatchService.run                : (시각·요일) 매칭, catch-up, 제외 규칙, dead 토큰 무효화.
    - DispatchService (dedup gate)        : _claim 이 None(중복)이면 발송 안 함.
    - internal.dispatch_calls            : 공유 시크릿 가드(미설정/불일치/일치).

환경 제약(반드시 모킹):
    - firebase-admin 미설치 → sys.modules 에 가짜 messaging 주입, _ensure_app 는 sentinel.
    - Postgres 전용 SQL(_claim/_purge ON CONFLICT, interval)은 SQLite 에서 안 돎 → monkeypatch.
    - datetime.now(APP_TZ) 는 FakeDatetime 로 고정('현재' 결정화). timedelta/timezone 은 실제.

인메모리 SQLite 는 test_normalcall_ws.py 패턴을 그대로 따른다.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# 전 모델 등록(부수효과) — Base.metadata 에 14개 테이블.
from db.registry import Base  # noqa: F401
from domains.account.models.member import Member
from domains.alarm.models.alarm import Alarm
from domains.alarm.models.schedule import Schedule
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.push.models.device_token import DeviceToken

import core.fcm as fcm_mod
from core.fcm import FcmSendResult, send_incoming_call
from core.push_defaults import DEFAULT_CALLER_NAME
import domains.push.service.dispatch_service as dsvc
from domains.push.service.dispatch_service import DispatchService, _DAY_CODES

APP_TZ = ZoneInfo("Asia/Seoul")


# --------------------------------------------------------------------------- #
# 인메모리 DB (BigInteger+Identity PK 는 sqlite autoincrement 안 됨 → Integer 치환)
# --------------------------------------------------------------------------- #
def _independent_factory():
    """격리된 인메모리 DB(엔진 1개 = StaticPool 단일 연결)의 세션 팩토리."""
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def session_factory():
    return _independent_factory()


_seed_counter = 0


def _seed(db, *, alarm_time, is_activate, day_codes, tokens):
    """member/voice/character/alarm(+schedules)/device_token 시드. ids 반환.

    tokens: [(token_str, is_valid), ...]  → DeviceToken(platform=android_fcm).
    day_codes: alarm.schedules 의 day_of_week 코드 집합.
    alarm_time: alarm.time 값(센티넬 날짜 + UTC 라벨) 또는 None.

    Voice.name / Member.auth_user_id 는 UNIQUE 라, 같은 DB 에 두 번 시드하는
    테스트를 위해 카운터로 유니크하게 만든다(StaticPool = 단일 인메모리 DB 공유).
    """
    global _seed_counter
    _seed_counter += 1
    n = _seed_counter
    voice = Voice(name=f"Fenrir-{n}", gender="male")
    db.add(voice)
    db.flush()
    ch = Character(
        name="비비", role="선생님", personality="다정",
        voice_id=voice.voice_id, price=0, image_url="https://img/beaver.png",
    )
    db.add(ch)
    db.flush()
    member = Member(language="en", korean_level=1, onboarding_completed=True,
                    auth_user_id=f"auth-member-{n}")
    db.add(member)
    db.flush()
    alarm = Alarm(member_id=member.member_id, character_id=ch.character_id,
                  time=alarm_time, is_activate=is_activate)
    db.add(alarm)
    db.flush()
    for code in day_codes:
        db.add(Schedule(alarm_id=alarm.alarm_id, day_of_week=code))
    for tok, valid in tokens:
        db.add(DeviceToken(member_id=member.member_id, platform="android_fcm",
                           token=tok, is_valid=valid))
    db.commit()
    return {"member_id": member.member_id, "character_id": ch.character_id,
            "alarm_id": alarm.alarm_id}


def _patch_now(monkeypatch, fixed: datetime):
    """dispatch_service.datetime 만 교체(now 고정). timedelta/timezone 은 그대로."""

    class FakeDatetime:
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(dsvc, "datetime", FakeDatetime)


def _sentinel_time(hour: int, minute: int) -> datetime:
    """alarm.time 실데이터 형태: 센티넬 날짜(2000-01-01) + UTC 라벨 벽시각."""
    return datetime(2000, 1, 1, hour, minute, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 가짜 firebase_admin.messaging 주입기 (scenario A 용)
# --------------------------------------------------------------------------- #
class _FakeMulticastMessage:
    def __init__(self, *, tokens=None, data=None, android=None, notification=None):
        self.tokens = tokens
        self.data = data
        self.android = android
        self.notification = notification  # data-only 검증용: 항상 None 이어야


class _FakeAndroidConfig:
    def __init__(self, *, priority=None, ttl=None):
        self.priority = priority
        self.ttl = ttl


@pytest.fixture()
def fake_messaging(monkeypatch):
    """가짜 firebase_admin + messaging 을 sys.modules 에 주입하고 컨트롤러 반환."""
    ns = ModuleType("firebase_admin.messaging")
    ns.MulticastMessage = _FakeMulticastMessage
    ns.AndroidConfig = _FakeAndroidConfig

    class UnregisteredError(Exception):
        pass

    class SenderIdMismatchError(Exception):
        pass

    ns.UnregisteredError = UnregisteredError
    ns.SenderIdMismatchError = SenderIdMismatchError

    state = SimpleNamespace(responses=[], captured_msg=None, captured_app=None,
                            send_calls=0)

    def send_each_for_multicast(msg, app=None):
        state.send_calls += 1
        state.captured_msg = msg
        state.captured_app = app
        return SimpleNamespace(responses=list(state.responses))

    ns.send_each_for_multicast = send_each_for_multicast

    fa = ModuleType("firebase_admin")
    fa.messaging = ns

    monkeypatch.setitem(sys.modules, "firebase_admin", fa)
    monkeypatch.setitem(sys.modules, "firebase_admin.messaging", ns)
    # 기본값: 앱은 정상(sentinel). None 케이스는 테스트에서 덮어씀.
    monkeypatch.setattr(fcm_mod, "_ensure_app", lambda: object())

    state.messaging = ns
    return state


def _resp(success, exception=None):
    return SimpleNamespace(success=success, exception=exception)


# =========================================================================== #
# A. core.fcm.send_incoming_call — data-only 페이로드 / 폐기 분류 / graceful
# =========================================================================== #
def test_fcm_payload_is_data_only_all_strings(fake_messaging):
    fake_messaging.responses = [_resp(True), _resp(True)]
    res = send_incoming_call(
        tokens=["tokA", "tokB"], call_id="c-1", character_id=7,
        name="BIBI", image_url="https://img/x.png",
    )
    msg = fake_messaging.captured_msg
    # data-only: notification 없음(가짜 클래스에 넘겼다면 값이 있었을 것)
    assert msg.notification is None
    assert set(msg.data.keys()) == {"call_id", "character_id", "name", "image_url"}
    assert all(isinstance(v, str) for v in msg.data.values())
    assert msg.data["character_id"] == "7"  # int → str
    assert msg.data["call_id"] == "c-1"
    # android high priority + 60s ttl(timedelta)
    assert msg.android.priority == "high"
    assert isinstance(msg.android.ttl, timedelta)
    assert msg.android.ttl == timedelta(seconds=60)
    # send_each_for_multicast 매핑: 성공 2건
    assert isinstance(res, FcmSendResult)
    assert res.sent == 2
    assert res.dead_tokens == []
    assert fake_messaging.captured_app is not None  # app 전달됨


def test_fcm_name_and_image_fallback(fake_messaging):
    fake_messaging.responses = [_resp(True)]
    send_incoming_call(tokens=["t"], call_id="c", character_id=1,
                       name=None, image_url=None)
    data = fake_messaging.captured_msg.data
    # 폴백은 core.push_defaults 가 단일 소스 — 문구를 하드코딩하면 또 어긋난다.
    assert data["name"] == DEFAULT_CALLER_NAME  # None → 기본 이름
    # ★ 30개 로케일 앱의 잠금화면에 뜨는 문구다. 한글이 섞이면 안 된다.
    assert data["name"].isascii(), "발신자 기본 이름에 비ASCII 문구가 들어감"
    assert data["image_url"] == ""      # None → 빈 문자열
    assert all(isinstance(v, str) for v in data.values())


def test_fcm_dead_token_classification(fake_messaging):
    ns = fake_messaging.messaging
    # tok1 성공, tok2 Unregistered(폐기), tok3 SenderIdMismatch(폐기), tok4 일시(유지)
    fake_messaging.responses = [
        _resp(True),
        _resp(False, ns.UnregisteredError("gone")),
        _resp(False, ns.SenderIdMismatchError("mismatch")),
        _resp(False, ValueError("transient network")),
    ]
    res = send_incoming_call(
        tokens=["tok1", "tok2", "tok3", "tok4"],
        call_id="c", character_id=1, name="n", image_url="",
    )
    assert res.sent == 1
    assert res.dead_tokens == ["tok2", "tok3"]  # 일시실패 tok4 는 폐기 아님


def test_fcm_no_app_is_graceful_no_send(fake_messaging, monkeypatch):
    monkeypatch.setattr(fcm_mod, "_ensure_app", lambda: None)
    res = send_incoming_call(tokens=["t"], call_id="c", character_id=1,
                             name="n", image_url="")
    assert isinstance(res, FcmSendResult)
    assert res.sent == 0 and res.dead_tokens == []
    assert fake_messaging.send_calls == 0  # 발송 시도 없음


def test_fcm_empty_tokens_no_send(fake_messaging):
    res = send_incoming_call(tokens=[], call_id="c", character_id=1,
                             name="n", image_url="")
    assert res.sent == 0
    assert fake_messaging.send_calls == 0


def test_fcm_send_exception_is_graceful(fake_messaging):
    def boom(msg, app=None):
        raise RuntimeError("fcm down")

    fake_messaging.messaging.send_each_for_multicast = boom
    res = send_incoming_call(tokens=["t"], call_id="c", character_id=1,
                             name="n", image_url="")
    assert res.sent == 0 and res.dead_tokens == []


# =========================================================================== #
# B. DispatchService.run — 매칭 / catch-up / 제외 / dead 토큰 무효화
#    (_claim/_purge 는 Postgres 전용이라 monkeypatch, fcm 는 가짜로 교체)
# =========================================================================== #
@pytest.fixture()
def patch_dispatch(monkeypatch):
    """_claim 성공(call_id 반환·기록), _purge no-op, fcm.send_incoming_call 가짜로 교체."""
    claims = []
    fcm_calls = []

    # _claim 은 성공 시 **발급한 call_id** 를, 중복이면 None 을 준다(bool 아님).
    # 이 값이 그대로 푸시 페이로드의 call_id 가 되고, 통화가 열릴 때 서버가
    # 그걸로 알람을 되짚어 캐릭터를 정한다.
    def fake_claim(self, alarm_id, bucket_key):
        claims.append((alarm_id, bucket_key))
        return f"call-{alarm_id}-{bucket_key}"

    monkeypatch.setattr(DispatchService, "_claim", fake_claim)
    monkeypatch.setattr(DispatchService, "_purge", lambda self: None)

    result_box = SimpleNamespace(result=FcmSendResult(sent=0, dead_tokens=[]))

    def fake_send(**kwargs):
        fcm_calls.append(kwargs)
        return result_box.result

    monkeypatch.setattr(fcm_mod, "send_incoming_call", fake_send)
    return SimpleNamespace(claims=claims, fcm_calls=fcm_calls, result_box=result_box)


def test_run_exact_minute_match_rings(session_factory, monkeypatch, patch_dispatch):
    now = datetime(2026, 7, 8, 8, 0, tzinfo=APP_TZ)  # 수요일 08:00 KST
    _patch_now(monkeypatch, now)
    today_code = _DAY_CODES[now.weekday()]
    db = session_factory()
    ids = _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True,
                day_codes={today_code}, tokens=[("tokA", True), ("tokB", True)])
    patch_dispatch.result_box.result = FcmSendResult(sent=2, dead_tokens=[])

    sent = DispatchService(db).run()

    assert sent == 2
    assert len(patch_dispatch.fcm_calls) == 1
    call = patch_dispatch.fcm_calls[0]
    assert sorted(call["tokens"]) == ["tokA", "tokB"]
    assert call["character_id"] == ids["character_id"]
    assert call["name"] == "비비"
    assert call["image_url"] == "https://img/beaver.png"
    # bucket_key = 정각 벽분
    assert patch_dispatch.claims == [(ids["alarm_id"], "2026-07-08 08:00")]
    db.close()


def test_run_catchup_window_rings_with_past_bucket_key(
    session_factory, monkeypatch, patch_dispatch
):
    now = datetime(2026, 7, 8, 8, 1, tzinfo=APP_TZ)  # 크론 1분 지연
    _patch_now(monkeypatch, now)
    monkeypatch.setattr(dsvc.settings, "INTERNAL_DISPATCH_CATCHUP_MIN", 1)
    code = _DAY_CODES[now.weekday()]
    db = session_factory()
    ids = _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True,
                day_codes={code}, tokens=[("t", True)])
    patch_dispatch.result_box.result = FcmSendResult(sent=1, dead_tokens=[])

    sent = DispatchService(db).run()

    assert sent == 1
    assert len(patch_dispatch.fcm_calls) == 1
    # 의도된 벽분 버킷은 08:00(과거 버킷), 08:01 아님
    assert patch_dispatch.claims == [(ids["alarm_id"], "2026-07-08 08:00")]
    db.close()


def test_run_wrong_weekday_excluded(session_factory, monkeypatch, patch_dispatch):
    now = datetime(2026, 7, 8, 8, 0, tzinfo=APP_TZ)  # 수요일
    _patch_now(monkeypatch, now)
    today = _DAY_CODES[now.weekday()]
    wrong = _DAY_CODES[(now.weekday() + 1) % 7]  # 다른 요일
    assert wrong != today
    db = session_factory()
    _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True,
          day_codes={wrong}, tokens=[("t", True)])

    sent = DispatchService(db).run()

    assert sent == 0
    assert patch_dispatch.fcm_calls == []
    db.close()


def test_run_inactive_alarm_excluded(session_factory, monkeypatch, patch_dispatch):
    now = datetime(2026, 7, 8, 8, 0, tzinfo=APP_TZ)
    _patch_now(monkeypatch, now)
    code = _DAY_CODES[now.weekday()]
    db = session_factory()
    # is_activate=False → SQL where 절에서 제외
    _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=False,
          day_codes={code}, tokens=[("t", True)])
    sent_false = DispatchService(db).run()
    db.close()

    db2 = _independent_factory()()  # is_activate=None 시나리오는 격리 DB 에서
    _seed(db2, alarm_time=_sentinel_time(8, 0), is_activate=None,
          day_codes={code}, tokens=[("t2", True)])
    sent_none = DispatchService(db2).run()
    db2.close()

    assert sent_false == 0
    assert sent_none == 0
    assert patch_dispatch.fcm_calls == []


def test_run_time_none_skipped(session_factory, monkeypatch, patch_dispatch):
    now = datetime(2026, 7, 8, 8, 0, tzinfo=APP_TZ)
    _patch_now(monkeypatch, now)
    code = _DAY_CODES[now.weekday()]
    db = session_factory()
    _seed(db, alarm_time=None, is_activate=True,
          day_codes={code}, tokens=[("t", True)])

    sent = DispatchService(db).run()

    assert sent == 0
    assert patch_dispatch.fcm_calls == []
    db.close()


def test_run_no_valid_tokens_contributes_zero(
    session_factory, monkeypatch, patch_dispatch
):
    now = datetime(2026, 7, 8, 8, 0, tzinfo=APP_TZ)
    _patch_now(monkeypatch, now)
    code = _DAY_CODES[now.weekday()]
    db = session_factory()
    # 토큰이 있지만 is_valid=False → _ring 은 유효 토큰 0 → fcm 미호출
    _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True,
          day_codes={code}, tokens=[("dead", False)])

    sent = DispatchService(db).run()

    assert sent == 0
    assert patch_dispatch.fcm_calls == []  # 유효 토큰 없으면 발송 자체 안 함
    db.close()


def test_run_dead_token_marks_invalid_and_commits(
    session_factory, monkeypatch, patch_dispatch
):
    now = datetime(2026, 7, 8, 8, 0, tzinfo=APP_TZ)
    _patch_now(monkeypatch, now)
    code = _DAY_CODES[now.weekday()]
    factory = session_factory
    db = factory()
    ids = _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True,
                day_codes={code}, tokens=[("live", True), ("gone", True)])
    # fcm 가 'gone' 을 폐기로 보고
    patch_dispatch.result_box.result = FcmSendResult(sent=1, dead_tokens=["gone"])

    sent = DispatchService(db).run()
    assert sent == 1
    db.close()

    # 새 세션으로 커밋 반영 확인
    db2 = factory()
    from sqlalchemy import select
    rows = {t.token: t.is_valid for t in
            db2.execute(select(DeviceToken)).scalars().all()}
    assert rows["gone"] is False   # 폐기 토큰 무효화 + 커밋
    assert rows["live"] is True
    db2.close()


# =========================================================================== #
# C. dedup gate — _claim 이 None(중복)이면 발송 안 함
# =========================================================================== #
def test_run_claim_false_skips_send(session_factory, monkeypatch):
    now = datetime(2026, 7, 8, 8, 0, tzinfo=APP_TZ)
    _patch_now(monkeypatch, now)
    # 중복 클레임 = None. ⚠ False 를 쓰면 안 된다 — `is not None` 검사를 통과해
    #   중복인데도 발송된다(계약이 bool → Optional[str] 로 바뀌었다).
    monkeypatch.setattr(DispatchService, "_claim", lambda self, aid, key: None)
    monkeypatch.setattr(DispatchService, "_purge", lambda self: None)
    fcm_calls = []
    monkeypatch.setattr(fcm_mod, "send_incoming_call",
                        lambda **k: fcm_calls.append(k) or FcmSendResult())
    code = _DAY_CODES[now.weekday()]
    db = session_factory()
    _seed(db, alarm_time=_sentinel_time(8, 0), is_activate=True,
          day_codes={code}, tokens=[("t", True)])

    sent = DispatchService(db).run()

    assert sent == 0
    assert fcm_calls == []  # 중복 클레임 → 발송 스킵
    db.close()


# =========================================================================== #
# D. internal.dispatch_calls — 공유 시크릿 가드
# =========================================================================== #
def test_internal_secret_unset_forbidden(monkeypatch):
    from fastapi import HTTPException
    import domains.push.routers.internal as internal

    monkeypatch.setattr(internal.settings, "INTERNAL_DISPATCH_SECRET", None)
    with pytest.raises(HTTPException) as ei:
        internal.dispatch_calls(db=None, x_internal_secret="whatever")
    assert ei.value.status_code == 403


def test_internal_secret_wrong_forbidden(monkeypatch):
    from fastapi import HTTPException
    import domains.push.routers.internal as internal

    monkeypatch.setattr(internal.settings, "INTERNAL_DISPATCH_SECRET", "s3cret")
    with pytest.raises(HTTPException) as ei:
        internal.dispatch_calls(db=None, x_internal_secret="nope")
    assert ei.value.status_code == 403


def test_internal_secret_correct_runs(monkeypatch):
    import domains.push.routers.internal as internal

    monkeypatch.setattr(internal.settings, "INTERNAL_DISPATCH_SECRET", "s3cret")
    monkeypatch.setattr(DispatchService, "run", lambda self: 3)
    out = internal.dispatch_calls(db=object(), x_internal_secret="s3cret")
    assert out == {"dispatched": 3}


# =========================================================================== #
# E. midnight weekday edge — catch-up 이 now 가 아니라 BUCKET 요일을 쓴다
# =========================================================================== #
def test_run_midnight_uses_bucket_weekday_not_now(
    session_factory, monkeypatch, patch_dispatch
):
    now = datetime(2026, 7, 13, 0, 0, tzinfo=APP_TZ)  # 월요일 00:00 KST
    _patch_now(monkeypatch, now)
    monkeypatch.setattr(dsvc.settings, "INTERNAL_DISPATCH_CATCHUP_MIN", 1)
    prev_bucket = (now - timedelta(minutes=1)).replace(second=0, microsecond=0)
    bucket_code = _DAY_CODES[prev_bucket.weekday()]  # 일요일(전날)
    now_code = _DAY_CODES[now.weekday()]             # 월요일
    assert bucket_code != now_code

    # (1) 스케줄이 BUCKET 요일(일)이면 링한다
    db = session_factory()
    _seed(db, alarm_time=_sentinel_time(23, 59), is_activate=True,
          day_codes={bucket_code}, tokens=[("t", True)])
    patch_dispatch.result_box.result = FcmSendResult(sent=1, dead_tokens=[])
    sent_bucket = DispatchService(db).run()
    key_bucket = list(patch_dispatch.claims)
    db.close()

    # (2) 스케줄이 now 요일(월)뿐이면 링하지 않는다(요일은 버킷 기준)
    patch_dispatch.claims.clear()
    patch_dispatch.fcm_calls.clear()
    db2 = _independent_factory()()  # 첫 시나리오 데이터가 새지 않게 격리 DB
    _seed(db2, alarm_time=_sentinel_time(23, 59), is_activate=True,
          day_codes={now_code}, tokens=[("t2", True)])
    sent_now = DispatchService(db2).run()
    db2.close()

    assert sent_bucket == 1
    assert key_bucket[0][1] == prev_bucket.strftime("%Y-%m-%d %H:%M")  # 전날 23:59
    assert sent_now == 0
