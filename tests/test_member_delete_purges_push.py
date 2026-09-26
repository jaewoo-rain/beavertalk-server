"""회원 탈퇴 — S3(예약 통화 누수) → S2(하드 삭제)로 이어진 member_service.delete().

S3(2026-09-26)에선 소프트 삭제가 alarm·device_token 을 안 지워 새는 것만 앱 코드로
bulk 삭제해 막았다. S2(같은 날, 뒤이어)가 delete() 자체를 **하드 삭제**로 바꿨다 —
`DELETE FROM member` 하나로 CASCADE FK(alarm·device_token·member_character 등)가
정리되고, 결제 3표(payment·subscribe·iap_receipt)만 `ON DELETE SET NULL`로 살아
남는다(법무 보존 의무). 이 파일은 그 최종 동작을 검증한다.

⚠ SQLite 는 기본적으로 FK ondelete 를 실행하지 않는다(`PRAGMA foreign_keys=ON` 필요)
— 이걸 안 켜면 raw `DELETE FROM member` 뒤 CASCADE·SET NULL 이 전혀 안 일어나
"통과했지만 아무것도 검증 안 한" 시험이 된다. 연결마다 켜지도록 이벤트 리스너를 단다.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Integer, create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base
from domains.account.models.member import Member
from domains.account.service import member_service as msvc
from domains.account.service.member_service import MemberService
from domains.alarm.models.alarm import Alarm
from domains.commerce.models.character import Character
from domains.commerce.models.iap_receipt import IapReceipt
from domains.commerce.models.member_character import MemberCharacter
from domains.commerce.models.payment import Payment
from domains.commerce.models.subscribe import Subscribe
from domains.commerce.models.voice import Voice
from domains.push.models.device_token import DeviceToken


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

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = sf()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _auth_ok(monkeypatch):
    """Supabase auth 삭제는 이 시험의 관심사가 아니다 — 항상 성공으로 둔다."""
    monkeypatch.setattr(msvc, "delete_auth_user", lambda uid: True)


def _seed_member_with_everything(db, *, n: int) -> dict:
    voice = Voice(name=f"Fenrir-{n}", gender="male")
    db.add(voice)
    db.flush()
    ch = Character(name="비비", role="선생님", personality="다정", voice_id=voice.voice_id, price=0)
    db.add(ch)
    db.flush()
    member = Member(language="en", korean_level=1, onboarding_completed=True,
                    auth_user_id=f"auth-{n}", email=f"m{n}@x.io")
    db.add(member)
    db.flush()
    mid = member.member_id
    db.add(Alarm(member_id=mid, character_id=ch.character_id, is_activate=True))
    db.add(DeviceToken(member_id=mid, platform="android_fcm", token=f"tok-{n}"))
    db.add(MemberCharacter(member_id=mid, character_id=ch.character_id))
    db.add(Payment(member_id=mid, price=9900, description="premium"))
    db.add(Subscribe(member_id=mid))
    db.add(IapReceipt(member_id=mid, platform="android", transaction_id=f"tx-{n}",
                      product_id="premium_monthly", kind="subscription"))
    db.commit()
    return {"member_id": mid, "character_id": ch.character_id}


def test_delete_hard_deletes_the_member_row(db):
    ids = _seed_member_with_everything(db, n=1)
    MemberService(db).delete(ids["member_id"])
    assert db.get(Member, ids["member_id"]) is None


def test_delete_cascades_alarm_device_token_and_owned_character(db):
    """CASCADE FK 3종 — 회원이 지워지면 같이 지워진다(테이블을 따로 지우는 코드 없이)."""
    ids = _seed_member_with_everything(db, n=1)
    mid = ids["member_id"]

    MemberService(db).delete(mid)

    assert db.query(Alarm).filter(Alarm.member_id == mid).count() == 0
    assert db.query(DeviceToken).filter(DeviceToken.member_id == mid).count() == 0
    assert db.query(MemberCharacter).filter(MemberCharacter.member_id == mid).count() == 0


def test_delete_sets_null_on_payment_subscribe_iap_receipt_not_deletes(db):
    """⛔⛔ 핵심 잠금 — 결제 3표는 SET NULL 이다. CASCADE 로 되돌아가면 결제 기록이
    조용히 사라진다(법무 보존 의무 위반) — 이 시험이 그 회귀를 잡는다."""
    ids = _seed_member_with_everything(db, n=1)
    mid = ids["member_id"]

    MemberService(db).delete(mid)

    payment = db.query(Payment).filter(Payment.description == "premium").one()
    subscribe = db.query(Subscribe).one()
    receipt = db.query(IapReceipt).filter(IapReceipt.transaction_id == "tx-1").one()
    assert payment.member_id is None
    assert subscribe.member_id is None
    assert receipt.member_id is None
    # 금액·상품 등 결제 내용 자체는 그대로 남아 있어야 한다(보존 의무의 요점).
    assert payment.price == 9900
    assert receipt.product_id == "premium_monthly"


def test_delete_does_not_touch_another_members_data(db):
    """회귀 — 한 회원 하드 삭제가 다른(살아있는) 회원의 데이터를 건드리면 안 된다."""
    mine = _seed_member_with_everything(db, n=1)
    other = _seed_member_with_everything(db, n=2)

    MemberService(db).delete(mine["member_id"])

    assert db.get(Member, other["member_id"]) is not None
    assert db.query(Alarm).filter(Alarm.member_id == other["member_id"]).count() == 1
    assert db.query(DeviceToken).filter(DeviceToken.member_id == other["member_id"]).count() == 1
    assert db.query(Subscribe).filter(Subscribe.member_id == other["member_id"]).one().member_id \
        == other["member_id"]


def test_delete_with_no_related_rows_is_a_noop_not_an_error(db):
    """관련 행이 원래 없는 회원도 delete() 가 에러 없이 통과해야 한다."""
    member = Member(language="en", korean_level=1, onboarding_completed=True,
                    auth_user_id="auth-bare", email="bare@x.io")
    db.add(member)
    db.commit()
    mid = member.member_id

    MemberService(db).delete(mid)

    assert db.get(Member, mid) is None


def test_delete_never_touches_storage():
    """⭐ 사장님 결정(2026-09-26 「음성 놔둬, 지우지마」) 잠금 — GCS 삭제 함수 자체가
    core/storage.py 에 없어야 한다. 누가 「정리」라며 delete 류를 추가하면 이 시험이
    잡는다(멀리 있는 다음 사람을 위한 회귀 방지, member_service.py 는 storage 를
    import 조차 하지 않는다)."""
    from core import storage

    suspicious = [
        name for name in dir(storage)
        if not name.startswith("_")
        and any(kw in name.lower() for kw in ("delete", "remove", "purge"))
    ]
    assert suspicious == [], f"core/storage.py 에 삭제류 함수가 생겼다: {suspicious}"
