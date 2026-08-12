"""IAP 영수증 검증·멱등·지급·복원 회귀 (외부 의존 0, 인메모리 sqlite).

계약: docs/20260731_1230_IAP-API-계약서-프론트공유용.md

핵심 불변식:
  - 같은 영수증이 여러 번 와도 **한 번만** 지급된다(멱등) — 재시도·복원은 정상 동작이다.
  - 422(무효)와 503(스토어 불통)을 구분한다 — 앱의 재시도 여부가 갈린다.
  - 지급 가격은 서버가 모른다(스토어가 정함) — purchase_price/price 는 NULL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.config import settings as app_settings
from db.registry import Base  # noqa: F401 - 전 모델 import
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.iap_receipt import IapReceipt
from domains.commerce.models.member_character import MemberCharacter
from domains.commerce.models.subscribe import Subscribe
from domains.commerce.models.voice import Voice
from domains.commerce.schemas.iap import PurchaseItem
from domains.commerce.service import iap_catalog
from domains.commerce.service.iap_service import IapService

# 3티어 재편(2026-08-04): 상품 ID 스킴을 앱(`IapProductIds`)과 같은 `bt_*` 로 통일.
# 캐릭터는 이름이 아니라 character.product_key(불변 슬러그)로 식별한다.
BIBI = "bt_character_bibi"
POPO = "bt_character_popo"
PRO = "bt_pro_monthly"
PRO_YEARLY = "bt_pro_yearly"
MAX = "bt_max_monthly"
MAX_YEARLY = "bt_max_yearly"


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
    for name in ("BIBI", "Popo"):
        s.add(Character(name=name, role="r", personality="p",
                        voice_id=v.voice_id, price=10))
    m = Member(language="en", onboarding_completed=True, auth_user_id="a1")
    s.add(m)
    s.commit()
    s.info["member_id"] = m.member_id
    return s


@pytest.fixture(autouse=True)
def _stub_on(monkeypatch):
    """자격증명이 없으므로 스텁 경로로 검증한다(계약은 실제와 동일)."""
    monkeypatch.setattr(app_settings, "IAP_VERIFY_ENABLED", False)
    monkeypatch.setattr(app_settings, "IAP_ALLOW_STUB", True)


def _item(product=BIBI, tx="tx-1", token="ok-token"):
    return PurchaseItem(product_id=product, transaction_id=tx, purchase_token=token)


def _mid(db):
    return db.info["member_id"]


# --------------------------------------------------------------------------- #
# 1) 상품 매핑 — id 하드코딩 금지(환경마다 character_id 가 다르다)
# --------------------------------------------------------------------------- #
def test_product_maps_by_product_key_not_hardcoded_id(db):
    """상품 ID 는 product_key(불변 슬러그)로 푼다 — character_id 도 name 도 아니다.

    character_id 는 dev/prod 가 다르고(prod 2·9·10·11 / dev 2·3·4·5), name 은 바뀔 수
    있는데 **스토어 상품 ID 는 영구 불변**이라 둘 다 영구 식별자로 못 쓴다.
    """
    ref = iap_catalog.resolve(db, BIBI)
    assert ref is not None and ref.kind == "character"
    bibi = db.query(Character).filter_by(name="BIBI").one()
    assert ref.character_id == bibi.character_id
    assert bibi.product_key == "bibi"  # name 에서 자동 파생


def test_product_key_survives_rename(db):
    """이름을 바꿔도 상품 ID 는 그대로 — 이 컬럼의 존재 이유."""
    bibi = db.query(Character).filter_by(name="BIBI").one()
    bibi.name = "비비 (개명)"
    db.commit()

    ref = iap_catalog.resolve(db, BIBI)
    assert ref is not None and ref.character_id == bibi.character_id


@pytest.mark.parametrize(
    "pid,plan,period",
    [
        (PRO, "pro", "monthly"),
        (PRO_YEARLY, "pro", "yearly"),
        (MAX, "max", "monthly"),
        (MAX_YEARLY, "max", "yearly"),
    ],
)
def test_subscription_products_map_to_plan_and_period(db, pid, plan, period):
    """구독 4종이 plan × 주기로 풀린다. 앱 IapProductIds 와 같은 문자열이어야 한다."""
    ref = iap_catalog.resolve(db, pid)
    assert ref is not None and ref.kind == "subscription"
    assert ref.character_id is None
    assert (ref.plan, ref.billing_period) == (plan, period)


@pytest.mark.parametrize(
    "pid",
    [
        "bt_character_nosuch",
        "wat",
        "im.beavertalk.pro.monthly",  # 구 스킴은 더 이상 안 받는다
        "bt_pro_weekly",              # 정의되지 않은 주기
    ],
)
def test_unknown_product_is_404(db, pid):
    with pytest.raises(HTTPException) as ex:
        IapService(db).verify_and_grant(_mid(db), "ios", _item(product=pid))
    assert ex.value.status_code == 404
    assert ex.value.detail["code"] == "UNKNOWN_PRODUCT"


# --------------------------------------------------------------------------- #
# 2) 검증 실패 — 422(무효) vs 503(불통) 구분
# --------------------------------------------------------------------------- #
def test_invalid_receipt_is_422(db):
    """재시도해도 소용없는 실패."""
    with pytest.raises(HTTPException) as ex:
        IapService(db).verify_and_grant(_mid(db), "ios", _item(token="invalid-xxx"))
    assert ex.value.status_code == 422
    assert ex.value.detail["code"] == "INVALID_RECEIPT"


def test_store_unavailable_is_503(db):
    """잠시 뒤 되는 실패 — 앱은 재시도 큐에 넣어야 한다."""
    with pytest.raises(HTTPException) as ex:
        IapService(db).verify_and_grant(_mid(db), "ios", _item(token="unavailable-x"))
    assert ex.value.status_code == 503
    assert ex.value.detail["code"] == "VERIFY_UNAVAILABLE"


def test_failed_verify_grants_nothing(db):
    with pytest.raises(HTTPException):
        IapService(db).verify_and_grant(_mid(db), "ios", _item(token="invalid-x"))
    assert db.query(MemberCharacter).count() == 0
    assert db.query(IapReceipt).count() == 0


# --------------------------------------------------------------------------- #
# 3) 지급 + 멱등
# --------------------------------------------------------------------------- #
def test_character_granted_once(db):
    r = IapService(db).verify_and_grant(_mid(db), "ios", _item())
    assert r.already_granted is False
    assert r.kind == "character"
    bibi = db.query(Character).filter_by(name="BIBI").one()
    assert r.character_id == bibi.character_id
    assert bibi.character_id in r.entitlement.owned_character_ids


def test_same_receipt_twice_is_idempotent(db):
    """★ 재시도·앱 재실행으로 같은 영수증이 또 온다 — 200 + already_granted."""
    svc = IapService(db)
    svc.verify_and_grant(_mid(db), "ios", _item())
    again = svc.verify_and_grant(_mid(db), "ios", _item())
    assert again.already_granted is True
    assert db.query(MemberCharacter).count() == 1, "중복 지급됨"
    assert db.query(IapReceipt).count() == 1


def test_receipt_of_another_member_is_409(db):
    svc = IapService(db)
    svc.verify_and_grant(_mid(db), "ios", _item())
    other = Member(language="en", onboarding_completed=True, auth_user_id="a2")
    db.add(other)
    db.commit()
    with pytest.raises(HTTPException) as ex:
        svc.verify_and_grant(other.member_id, "ios", _item())
    assert ex.value.status_code == 409
    assert ex.value.detail["code"] == "RECEIPT_OWNED_BY_OTHER"


def test_price_is_not_recorded(db):
    """가격은 스토어가 정한다 — 서버가 금액을 지어내면 안 된다."""
    IapService(db).verify_and_grant(_mid(db), "ios", _item())
    assert db.query(MemberCharacter).one().purchase_price is None


def test_stub_flag_recorded(db):
    """스텁 지급은 표시가 남아야 운영 정산에서 걸러낼 수 있다."""
    IapService(db).verify_and_grant(_mid(db), "ios", _item())
    assert db.query(IapReceipt).one().is_stub is True


# --------------------------------------------------------------------------- #
# 4) 구독
# --------------------------------------------------------------------------- #
def test_subscription_grants_pro(db):
    r = IapService(db).verify_and_grant(_mid(db), "android", _item(product=PRO, tx="s1"))
    assert r.kind == "subscription"
    assert r.character_id is None
    assert r.entitlement.is_pro is True
    assert r.entitlement.pro_expires_at is not None


def test_expired_subscription_is_not_pro(db):
    """만료 판정은 서버가 한다 — 앱 시계를 믿지 않는다."""
    now = datetime.now(timezone.utc)
    db.add(Subscribe(member_id=_mid(db), start_date=now - timedelta(days=60),
                     end_date=now - timedelta(days=1), is_activate=True))
    db.commit()
    ent = IapService(db).entitlement(_mid(db))
    assert ent.is_pro is False
    assert ent.pro_expires_at is None


def test_resubscribe_extends_not_duplicates(db):
    svc = IapService(db)
    svc.verify_and_grant(_mid(db), "ios", _item(product=PRO, tx="s1"))
    svc.verify_and_grant(_mid(db), "ios", _item(product=PRO, tx="s2"))
    assert db.query(Subscribe).count() == 1, "구독 행이 중복 생성됨"


# --------------------------------------------------------------------------- #
# 5) 복원 — 폰 교체·재설치
# --------------------------------------------------------------------------- #
def test_restore_grants_valid_and_reports_failed(db):
    items = [
        _item(tx="r1"),
        _item(product=POPO, tx="r2"),
        _item(tx="r3", token="invalid-x"),  # 무효 1건
    ]
    res = IapService(db).restore(_mid(db), "ios", items)
    assert res.restored == 2
    assert res.failed == 1
    assert len(res.entitlement.owned_character_ids) == 2


def test_restore_after_reinstall_is_idempotent(db):
    """이미 가진 것을 복원해도 중복 지급되지 않는다."""
    svc = IapService(db)
    svc.verify_and_grant(_mid(db), "ios", _item(tx="r1"))
    res = svc.restore(_mid(db), "ios", [_item(tx="r1")])
    assert res.failed == 0
    assert db.query(MemberCharacter).count() == 1


# --------------------------------------------------------------------------- #
# 6) 안전장치 — 실검증도 스텁도 꺼지면 결제를 받지 않는다
# --------------------------------------------------------------------------- #
def test_no_verify_no_stub_rejects(db, monkeypatch):
    monkeypatch.setattr(app_settings, "IAP_ALLOW_STUB", False)
    with pytest.raises(HTTPException) as ex:
        IapService(db).verify_and_grant(_mid(db), "ios", _item())
    assert ex.value.status_code == 503
