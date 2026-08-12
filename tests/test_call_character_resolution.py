"""통화 캐릭터를 **서버가 정한다** — 해석 규칙 회귀 (외부 의존 0, 인메모리 sqlite).

배경(prod 실측):
  - 통화 701 건 중 421 건(60%)이 사용자가 고른 캐릭터가 아닌 상대와 연결됐다.
    앱의 call_loading 이 `args is int ? args : 1` 로 폴백해, 인자를 안 넘기는
    진입점(마이페이지·기록·온보딩완료)에서 항상 1(BABA)을 보냈기 때문.
  - 소유 검증이 아예 없어 미구매 Bibi($10)로 126 건이 진행됐다.

둘 다 뿌리가 "캐릭터를 클라가 정한다" 라서, start.character_id 통로를 닫고
서버가 두 출처에서만 읽게 했다:
    수신통화(알람) inbound_call_id → push_dispatch_log → alarm.character_id
    그 외         member.character_id (소유 확인)

핵심 불변식:
  - 클라가 무엇을 보내든 통화 캐릭터를 **고를 수 없다**.
  - 남의 알람 uuid 로 남의 캐릭터를 열 수 없다.
  - 어떤 경우에도 연결을 끊지 않는다(거절이 아니라 폴백) — 통화는 사용자가 이미
    마이크를 켜고 기다리는 순간이다.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401 - 전 모델 import
from domains.account.models.member import Member
from domains.alarm.models.alarm import Alarm
from domains.commerce.models.character import Character
from domains.commerce.models.member_character import MemberCharacter
from domains.commerce.models.voice import Voice
from domains.learning.service.normalcall_service import resolve_call_character
from domains.push.models.push_dispatch_log import PushDispatchLog


@pytest.fixture()
def db():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    v = Voice(name="Leda", gender="female")
    s.add(v)
    s.flush()
    # 무료 BABA(폴백 종착지) + 유료 BIBI/Rara. id 를 하드코딩하지 않고 이름으로 집는다.
    for name, price in (("BABA", 0), ("BIBI", 10), ("Rara", 10)):
        s.add(Character(name=name, role="r", personality="p",
                        voice_id=v.voice_id, price=price))
    s.commit()
    return s


def _cid(db, name: str) -> int:
    return db.query(Character).filter_by(name=name).one().character_id


def _member(db, selected: str | None = None, owns: tuple[str, ...] = ()) -> int:
    m = Member(language="en", onboarding_completed=True,
               auth_user_id=f"a{db.query(Member).count() + 1}")
    if selected:
        m.character_id = _cid(db, selected)
    db.add(m)
    db.flush()
    for name in owns:
        db.add(MemberCharacter(member_id=m.member_id, character_id=_cid(db, name)))
    db.commit()
    return m.member_id


def _dispatched(db, member_id: int, character: str, call_id: str) -> None:
    """이 회원에게 `character` 알람 전화를 발송한 상태를 만든다."""
    a = Alarm(member_id=member_id, character_id=_cid(db, character),
              time=datetime.now(timezone.utc), is_activate=True)
    db.add(a)
    db.flush()
    db.add(PushDispatchLog(alarm_id=a.alarm_id,
                           intended_fire_minute="2026-07-31 08:00",
                           call_id=call_id))
    db.commit()


# --------------------------------------------------------------------------- #
# 1) 일반 통화 — 대표 캐릭터(member.character_id)
# --------------------------------------------------------------------------- #
def test_uses_selected_character_when_no_inbound(db):
    mid = _member(db, selected="Rara", owns=("BABA", "Rara"))
    assert resolve_call_character(db, mid) == _cid(db, "Rara")


def test_unowned_selected_falls_back_to_free(db):
    """고르기만 하고 안 산 상태(member.character_id 는 소유와 별개인 FK)."""
    mid = _member(db, selected="BIBI", owns=("BABA",))
    assert resolve_call_character(db, mid) == _cid(db, "BABA")


def test_no_selection_falls_back_to_cheapest(db):
    mid = _member(db, selected=None, owns=())
    assert resolve_call_character(db, mid) == _cid(db, "BABA")


# --------------------------------------------------------------------------- #
# 2) 수신통화 — 알람의 캐릭터(대표와 달라도 알람이 이긴다)
# --------------------------------------------------------------------------- #
def test_inbound_uses_alarm_character_not_selected(db):
    """★ 이게 핵심 — 알람마다 캐릭터가 다를 수 있다."""
    mid = _member(db, selected="BABA", owns=("BABA", "Rara"))
    _dispatched(db, mid, "Rara", "call-abc")
    assert resolve_call_character(db, mid, "call-abc") == _cid(db, "Rara")


def test_unknown_inbound_id_falls_back(db):
    """로그가 purge 됐거나 컬럼 추가 이전 발송 — 끊지 말고 대표 캐릭터로."""
    mid = _member(db, selected="Rara", owns=("BABA", "Rara"))
    assert resolve_call_character(db, mid, "call-없음") == _cid(db, "Rara")


def test_other_members_inbound_id_is_rejected(db):
    """★ 남의 알람 uuid 로 남의 캐릭터를 열 수 없다."""
    victim = _member(db, selected="Rara", owns=("BABA", "Rara"))
    _dispatched(db, victim, "Rara", "call-victim")
    attacker = _member(db, selected="BABA", owns=("BABA",))
    assert resolve_call_character(db, attacker, "call-victim") == _cid(db, "BABA")


# --------------------------------------------------------------------------- #
# 3) 통로 자체가 닫혔다 — 클라는 캐릭터를 고를 수 없다
# --------------------------------------------------------------------------- #
def test_client_cannot_choose_character(db):
    """resolve 는 클라가 준 character_id 를 받는 인자가 아예 없다.

    (protocol.ClientStart.character_id 는 구버전 앱 호환으로 남겨두되 서버가 무시한다.)
    """
    import inspect

    params = set(inspect.signature(resolve_call_character).parameters)
    assert params == {"db", "member_id", "inbound_call_id"}, (
        "클라가 캐릭터를 지정할 수 있는 인자가 생겼다 — 통로를 다시 열면 안 된다"
    )


def test_never_raises_when_no_characters_exist(db):
    """캐릭터 테이블이 비어도 통화 연결을 끊지 않는다."""
    mid = _member(db)
    db.query(MemberCharacter).delete()
    db.query(Character).delete()
    db.commit()
    assert resolve_call_character(db, mid) == 1


# --------------------------------------------------------------------------- #
# 5) Max 구독 — 카탈로그 전체가 열린다(소유 없이도 통화 가능)
# --------------------------------------------------------------------------- #
# ⛔ 여기서 지키는 것: Max 는 **접근**을 열지 소유를 주지 않는다. member_character 행이
#    생기면 해지 후에도 영구 소유가 되어 되돌릴 수 없다.
def _subscribe(db, member_id: int, plan: str, *, days: int = 30,
               is_activate: bool = True, billing_state: str = "ok") -> None:
    from datetime import timedelta

    from domains.commerce.models.subscribe import Subscribe

    now = datetime.now(timezone.utc)
    db.add(Subscribe(
        member_id=member_id, plan=plan, start_date=now,
        end_date=now + timedelta(days=days), is_activate=is_activate,
        billing_state=billing_state, is_trial=False, source="manual",
    ))
    db.commit()


def test_max_can_call_unowned_selected_character(db):
    """★ Max 회원은 안 산 캐릭터를 대표로 걸어도 그 캐릭터로 통화한다."""
    mid = _member(db, selected="BIBI", owns=("BABA",))
    _subscribe(db, mid, "max")
    assert resolve_call_character(db, mid) == _cid(db, "BIBI")


def test_max_unlock_creates_no_ownership_row(db):
    """⛔ 통화를 열어줬다고 소유 행이 생기면 안 된다 — 해지해도 안 잠기게 된다."""
    mid = _member(db, selected="BIBI", owns=("BABA",))
    _subscribe(db, mid, "max")
    resolve_call_character(db, mid)
    owned = db.query(MemberCharacter).filter_by(member_id=mid).all()
    assert [o.character_id for o in owned] == [_cid(db, "BABA")], "소유 행이 늘었다"


def test_pro_does_not_unlock_characters(db):
    """Pro 는 길이·횟수만 연다. 캐릭터는 Max 전용이라 종전대로 폴백한다."""
    mid = _member(db, selected="BIBI", owns=("BABA",))
    _subscribe(db, mid, "pro")
    assert resolve_call_character(db, mid) == _cid(db, "BABA")


def test_expired_max_relocks_characters(db):
    """★ 해지·만료하면 다시 잠긴다 — 앱의 downgradeWarning 이 약속한 동작."""
    mid = _member(db, selected="BIBI", owns=("BABA",))
    _subscribe(db, mid, "max", days=-1)   # 어제 끝난 구독
    assert resolve_call_character(db, mid) == _cid(db, "BABA")


def test_grace_max_keeps_characters(db):
    """grace(결제 재시도 중)는 접근 유지 — 카드 갱신하는 며칠 동안 뺏으면 안 된다."""
    mid = _member(db, selected="BIBI", owns=("BABA",))
    _subscribe(db, mid, "max", billing_state="grace")
    assert resolve_call_character(db, mid) == _cid(db, "BIBI")


def test_on_hold_max_relocks_characters(db):
    """on_hold(유예도 끝남)는 접근 차단 — grace 와의 비대칭이 요점이다."""
    mid = _member(db, selected="BIBI", owns=("BABA",))
    _subscribe(db, mid, "max", billing_state="on_hold")
    assert resolve_call_character(db, mid) == _cid(db, "BABA")


def test_max_still_honors_alarm_character(db):
    """Max 라도 수신통화는 알람 캐릭터가 이긴다(해석 순서 불변)."""
    mid = _member(db, selected="BIBI", owns=())
    _subscribe(db, mid, "max")
    _dispatched(db, mid, "Rara", "call-max-1")
    assert resolve_call_character(db, mid, "call-max-1") == _cid(db, "Rara")
