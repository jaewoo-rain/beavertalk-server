"""무료 캐릭터는 처음부터 보유 + 무료 지급 구매 API 삭제 (2026-09-29 사장님 지시).

⭐ 방식 = **파생 계산**(행을 만들지 않는다) — entitlements.is_free_character.
  가격 0원 캐릭터는 member_character 행이 없어도 is_owned=true · unlock_source="owned" 이고,
  통화 시작의 소유 검증(resolve_call_character)도 같은 함수를 본다.

⛔ 옛 사고: Bibi(0원)를 **고르기만** 하고 행이 없으면 통화 시작에서 조용히 가장 싼
  캐릭터(=Baba)로 바뀌었다. 여기서 회귀로 고정한다.
⛔ POST /characters/{id}/purchase 는 돈을 받지 않고 지급했다 — 삭제. 유료는
  POST /purchases/verify(스토어 영수증)로만.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401 - 전 모델 import(ORM 매퍼 초기화)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.member_character import MemberCharacter
from domains.commerce.models.voice import Voice
from domains.commerce.service import entitlements
from domains.commerce.service.character_service import CharacterService
from domains.learning.service.normalcall_service import resolve_call_character


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
    # 운영과 같은 모양: Baba·Bibi 0원, Popo 4.99
    for name, price in (("Baba", "0"), ("Bibi", "0"), ("Popo", "4.99")):
        s.add(Character(name=name, role="r", personality="p",
                        voice_id=v.voice_id, price=Decimal(price)))
    s.commit()
    return s


def _cid(db, name: str) -> int:
    return db.query(Character).filter_by(name=name).one().character_id


def _member(db, *, selected: str | None = None, owns: tuple[str, ...] = ()) -> int:
    m = Member(language="en", onboarding_completed=True,
               auth_user_id=f"a{db.query(Member).count() + 1}",
               character_id=_cid(db, selected) if selected else None)
    db.add(m)
    db.flush()
    for name in owns:
        db.add(MemberCharacter(member_id=m.member_id, character_id=_cid(db, name),
                               purchase_price=Decimal("0"),
                               purchase_date=datetime(2026, 8, 1, tzinfo=timezone.utc)))
    db.commit()
    return m.member_id


def _catalog(db, mid):
    return {c.name: c for c in CharacterService(db).list_characters(mid)}


# --------------------------------------------------------------------------- #
# 판정
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("price,free", [
    (Decimal("0"), True), (Decimal("0.00"), True), (0, True),
    (Decimal("4.99"), False), (None, False),
])
def test_is_free_character(price, free):
    class _C:
        pass
    c = _C()
    c.price = price
    assert entitlements.is_free_character(c) is free


def test_is_free_character_none_object_is_false():
    """db.get 이 None(지워진 캐릭터)을 줘도 죽지 않고 무료가 아니다."""
    assert entitlements.is_free_character(None) is False


# --------------------------------------------------------------------------- #
# 카탈로그 · 상세 · 내 캐릭터
# --------------------------------------------------------------------------- #
def test_free_characters_owned_without_rows(db):
    mid = _member(db)
    cat = _catalog(db, mid)
    for name in ("Baba", "Bibi"):
        assert (cat[name].is_owned, cat[name].is_unlocked, cat[name].unlock_source) == (
            True, True, "owned"), name
    assert (cat["Popo"].is_owned, cat["Popo"].is_unlocked, cat["Popo"].unlock_source) == (
        False, False, None), "유료 캐릭터가 열렸다"


def test_detail_matches_catalog_for_free_and_paid(db):
    mid = _member(db)
    svc = CharacterService(db)
    for name in ("Bibi", "Popo"):
        d = svc.get_character(mid, _cid(db, name))
        s = _catalog(db, mid)[name]
        assert (d.is_owned, d.is_unlocked, d.unlock_source) == (
            s.is_owned, s.is_unlocked, s.unlock_source), name


def test_reads_never_write_rows(db):
    mid = _member(db)
    _catalog(db, mid)
    CharacterService(db).get_character(mid, _cid(db, "Bibi"))
    CharacterService(db).list_owned(mid)
    assert db.query(MemberCharacter).filter_by(member_id=mid).count() == 0


def test_my_characters_lists_free_ones_without_rows(db):
    mid = _member(db)
    out = CharacterService(db).list_owned(mid)
    assert [o.name for o in out] == ["Baba", "Bibi"]
    assert all(o.purchase_price == Decimal("0") and o.purchase_date is None for o in out)


def test_existing_legacy_rows_give_same_result_and_no_duplicates(db):
    """기존 75행(0원 행 포함)이 있어도 결과가 같다 — 행의 구매 정보는 살아 있다."""
    with_rows = _member(db, owns=("Baba", "Bibi"))
    without = _member(db)
    a, b = _catalog(db, with_rows), _catalog(db, without)
    for name in ("Baba", "Bibi", "Popo"):
        assert (a[name].is_owned, a[name].unlock_source) == (b[name].is_owned, b[name].unlock_source)
    owned = CharacterService(db).list_owned(with_rows)
    assert [o.name for o in owned] == ["Baba", "Bibi"]
    assert owned[0].purchase_date is not None  # 행의 날짜를 그대로 쓴다


def test_paid_purchase_still_listed_with_free_ones(db):
    mid = _member(db)
    db.add(MemberCharacter(member_id=mid, character_id=_cid(db, "Popo"),
                           purchase_price=None, purchase_date=None))
    db.commit()
    assert [o.name for o in CharacterService(db).list_owned(mid)] == ["Baba", "Bibi", "Popo"]
    assert _catalog(db, mid)["Popo"].is_owned is True


# --------------------------------------------------------------------------- #
# 통화 시작 — 고른 무료 캐릭터가 Baba 로 바뀌지 않는다
# --------------------------------------------------------------------------- #
def test_selected_free_character_is_not_replaced_by_cheapest(db):
    """⛔⛔ 옛 사고 고정 — Bibi 를 고르기만 한(행 없음) 회원이 Baba 와 통화하게 되던 것."""
    mid = _member(db, selected="Bibi")
    assert resolve_call_character(db, mid).character_id == _cid(db, "Bibi")


def test_selected_unowned_paid_character_still_falls_back(db):
    """유료는 여전히 잠김 — 안 산 Popo 를 골라도 가장 싼 캐릭터로 간다."""
    mid = _member(db, selected="Popo")
    assert resolve_call_character(db, mid).character_id == _cid(db, "Baba")


# --------------------------------------------------------------------------- #
# 구매 API 삭제
# --------------------------------------------------------------------------- #
def test_purchase_route_is_gone():
    import main

    # 이 FastAPI 는 include_router 를 중첩(_IncludedRouter)으로 두어 app.routes 가 평평하지
    # 않다 — 앱이 실제로 보는 계약인 OpenAPI 경로로 확인한다.
    paths = set(main.app.openapi()["paths"])
    assert not any(p.endswith("/characters/{character_id}/purchase") for p in paths)
    assert any(p.endswith("/characters/{character_id}") for p in paths)  # 상세는 남는다
    assert any(p.endswith("/purchases/verify") for p in paths)  # 유료 지급 통로는 이것뿐


def test_purchase_service_module_is_gone():
    import importlib.util

    assert importlib.util.find_spec("domains.commerce.service.purchase_service") is None
    assert importlib.util.find_spec("domains.commerce.schemas.purchase") is None


# --------------------------------------------------------------------------- #
# 가입 — 스타터 member_character 행을 만들지 않는다(2026-09-29, ②의 꼬리)
# --------------------------------------------------------------------------- #
def test_signup_creates_no_ownership_rows_but_free_characters_work(db):
    """⭐ 신규 회원: 행 0개로도 무료 캐릭터가 보유이고, 대표 캐릭터가 정해지고, 통화 캐릭터가 그것이다."""
    from domains.account.service.member_service import MemberService

    m = MemberService(db).find_or_create_by_auth("auth-new-1", "new1@example.com")
    assert db.query(MemberCharacter).filter_by(member_id=m.member_id).count() == 0
    assert m.character_id == _cid(db, "Baba"), "대표 캐릭터(가장 낮은 id)가 안 정해졌다"
    cat = _catalog(db, m.member_id)
    assert cat["Baba"].is_owned and cat["Bibi"].is_owned and not cat["Popo"].is_owned
    assert resolve_call_character(db, m.member_id).character_id == _cid(db, "Baba")

    m.character_id = _cid(db, "Bibi")  # 무료 캐릭터를 골랐다
    db.commit()
    assert resolve_call_character(db, m.member_id).character_id == _cid(db, "Bibi")
