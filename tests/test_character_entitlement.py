"""Max 구독의 "모든 캐릭터" 혜택 — 소유(ownership)와 접근(entitlement)의 분리.

⛔ 이 파일이 지키는 핵심 하나: **Max 는 접근을 열지 소유를 주지 않는다.**
   섞으면 두 방향으로 터진다 —
     · member_character 행을 만들면 → 해지해도 영구 소유(되돌릴 수 없음)
     · is_owned 를 true 로 내보내면 → 앱이 "Owned" 배지를 띄우고 구매 CTA 를 숨긴다.
       샀다고 오해시킨 뒤 해지 때 뺏는 꼴이다(앱의 downgradeWarning 이 이미
       "Max-only characters turn off on {date}" 라고 말한다).

   그래서 wire 는 두 축이다: is_owned(영구 구매) + is_unlocked/unlock_source(지금 쓸 수 있나).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401 - 전 모델 import(ORM 매퍼 초기화)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.member_character import MemberCharacter
from domains.commerce.models.subscribe import Subscribe
from domains.commerce.models.voice import Voice
from domains.commerce.service import entitlements
from domains.commerce.service.character_service import CharacterService


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
    for name, price in (("BABA", 0), ("BIBI", 10), ("Rara", 10)):
        s.add(Character(name=name, role="r", personality="p",
                        voice_id=v.voice_id, price=Decimal(price)))
    s.commit()
    return s


def _cid(db, name: str) -> int:
    return db.query(Character).filter_by(name=name).one().character_id


def _member(db, owns: tuple[str, ...] = ()) -> int:
    m = Member(language="en", onboarding_completed=True,
               auth_user_id=f"a{db.query(Member).count() + 1}")
    db.add(m)
    db.flush()
    for name in owns:
        db.add(MemberCharacter(member_id=m.member_id, character_id=_cid(db, name)))
    db.commit()
    return m.member_id


def _subscribe(db, member_id: int, plan: str, *, days: int = 30,
               is_activate: bool = True, billing_state: str = "ok",
               is_trial: bool = False) -> None:
    now = datetime.now(timezone.utc)
    db.add(Subscribe(
        member_id=member_id, plan=plan, start_date=now,
        end_date=now + timedelta(days=days), is_activate=is_activate,
        billing_state=billing_state, is_trial=is_trial, source="manual",
    ))
    db.commit()


def _catalog(db, member_id: int) -> dict[str, object]:
    return {c.name: c for c in CharacterService(db).list_characters(member_id)}


# --------------------------------------------------------------------------- #
# 1) 판정 — 어떤 플랜이 카탈로그를 여는가
# --------------------------------------------------------------------------- #
def test_only_max_unlocks_all_characters():
    assert entitlements.unlocks_all_characters("max") is True
    assert entitlements.unlocks_all_characters("pro") is False
    assert entitlements.unlocks_all_characters(None) is False


@pytest.mark.parametrize(
    "kwargs,plan,unlocked",
    [
        ({}, "max", True),                                  # active_max
        ({}, "pro", False),                                 # active_pro
        ({"billing_state": "grace"}, "max", True),          # 결제 재시도 중 — 접근 유지
        ({"is_activate": False}, "max", True),              # ending — 기간 남음
        ({"is_trial": True}, "max", True),                  # 체험도 Max 취급
        ({"billing_state": "on_hold"}, "max", False),       # 유예도 끝남 — 차단
        ({"days": -1}, "max", False),                       # expired
    ],
)
def test_unlock_follows_subscription_state(db, kwargs, plan, unlocked):
    mid = _member(db)
    _subscribe(db, mid, plan, **kwargs)
    assert entitlements.has_all_characters(db, mid) is unlocked


def test_free_member_has_nothing_unlocked(db):
    assert entitlements.has_all_characters(db, _member(db)) is False


# --------------------------------------------------------------------------- #
# 2) 카탈로그 계약 — is_owned 와 is_unlocked 는 다른 축
# --------------------------------------------------------------------------- #
def test_max_unlocks_catalog_without_claiming_ownership(db):
    """★ 핵심. Max 회원의 미구매 캐릭터: 쓸 수 있지만(is_unlocked) 산 건 아니다(is_owned)."""
    mid = _member(db, owns=("BABA",))
    _subscribe(db, mid, "max")
    cat = _catalog(db, mid)

    assert cat["BIBI"].is_owned is False, "안 산 캐릭터를 샀다고 했다"
    assert cat["BIBI"].is_unlocked is True, "Max 인데 안 열렸다"
    assert cat["BIBI"].unlock_source == "subscription"


def test_owned_beats_subscription_as_unlock_source(db):
    """⛔ 둘 다면 'owned' 다 — 해지해도 남는 캐릭터를 해지 경고에 넣으면 안 된다."""
    mid = _member(db, owns=("BABA",))
    _subscribe(db, mid, "max")
    assert _catalog(db, mid)["BABA"].unlock_source == "owned"


def test_free_member_catalog_locks_unowned(db):
    mid = _member(db, owns=("BABA",))
    cat = _catalog(db, mid)

    assert (cat["BABA"].is_unlocked, cat["BABA"].unlock_source) == (True, "owned")
    assert (cat["BIBI"].is_unlocked, cat["BIBI"].unlock_source) == (False, None)


def test_pro_member_catalog_locks_unowned(db):
    """Pro 는 길이·횟수만 연다 — 캐릭터는 Max 전용."""
    mid = _member(db, owns=("BABA",))
    _subscribe(db, mid, "pro")
    assert _catalog(db, mid)["BIBI"].is_unlocked is False


def test_detail_matches_catalog(db):
    """목록과 상세가 다른 답을 하면 카드에선 열려 보이고 상세에선 잠긴다."""
    mid = _member(db, owns=("BABA",))
    _subscribe(db, mid, "max")
    detail = CharacterService(db).get_character(mid, _cid(db, "BIBI"))
    summary = _catalog(db, mid)["BIBI"]
    assert (detail.is_owned, detail.is_unlocked, detail.unlock_source) == (
        summary.is_owned, summary.is_unlocked, summary.unlock_source
    )


def test_unlock_never_writes_ownership_rows(db):
    """⛔ 조회가 소유 행을 만들면 안 된다 — 한 번 생기면 해지해도 안 잠긴다."""
    mid = _member(db, owns=("BABA",))
    _subscribe(db, mid, "max")
    _catalog(db, mid)
    CharacterService(db).get_character(mid, _cid(db, "BIBI"))
    rows = db.query(MemberCharacter).filter_by(member_id=mid).all()
    assert [r.character_id for r in rows] == [_cid(db, "BABA")]


def test_downgrade_relocks_catalog(db):
    """★ 해지 후 재조회하면 잠긴다 — 파생 계산이라 별도 정리 작업이 필요 없다."""
    mid = _member(db, owns=("BABA",))
    _subscribe(db, mid, "max")
    assert _catalog(db, mid)["BIBI"].is_unlocked is True

    sub = db.query(Subscribe).filter_by(member_id=mid).one()
    sub.end_date = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()

    cat = _catalog(db, mid)
    assert cat["BIBI"].is_unlocked is False, "만료됐는데 아직 열려 있다"
    assert cat["BABA"].is_unlocked is True, "산 캐릭터까지 잠겼다"


def test_old_app_sees_no_behavior_change(db):
    """구버전 앱은 is_unlocked 를 모르고 is_owned 만 본다 — 그 값이 안 변해야 한다.

    새 필드 추가가 하위호환인 이유를 값으로 못박는다(합쳤다면 여기가 깨진다).
    """
    mid = _member(db, owns=("BABA",))
    _subscribe(db, mid, "max")
    owned_flags = {n: c.is_owned for n, c in _catalog(db, mid).items()}
    assert owned_flags == {"BABA": True, "BIBI": False, "Rara": False}


# --------------------------------------------------------------------------- #
# 3) R5 — 구독 조회가 죽어도 서비스는 산다
# --------------------------------------------------------------------------- #
def test_lookup_failure_falls_back_to_locked(monkeypatch, db):
    """모르면 잠근다 — 조회 실패로 유료 캐릭터가 공짜로 열리면 안 된다."""
    def _boom(_db):
        raise RuntimeError("구독 테이블 장애")

    monkeypatch.setattr(
        "domains.commerce.repository.subscribe_repository.SubscribeRepository", _boom
    )
    mid = _member(db, owns=("BABA",))
    _subscribe(db, mid, "max")
    assert entitlements.has_all_characters(db, mid) is False
    assert _catalog(db, mid)["BIBI"].is_unlocked is False
