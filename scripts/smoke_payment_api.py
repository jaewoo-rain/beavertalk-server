"""payment 페이지 end-to-end 스모크 (sqlite).

구독 시작 + 캐릭터 구매 → 결제 내역(전체/구독/캐릭터 탭) + 이번 달 합계 검증.
"""

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import main
from db.registry import Base
from db.session import get_db
from domains.account.models.speak_country import SpeakCountry
from domains.commerce.models.character import Character

for _t in Base.metadata.tables.values():
    for _pk in _t.primary_key.columns:
        _pk.type = Integer()

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(engine)
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


main.app.dependency_overrides[get_db] = override_get_db
client = TestClient(main.app)

with TestingSessionLocal() as db:
    ch = Character(name="비비", price=Decimal("9.90"), image_url="b.png")
    db.add(ch)
    db.commit()
    ch_id = ch.character_id

from scripts.smoke_signup_helper import signup  # noqa: E402

signup(client, "pay@bt.io", password="pw")
tok = client.post("/api/v1/auth/login", data={"username": "pay@bt.io", "password": "pw"}).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}

# 1) 구독 시작 → 결제(subscribe) 생성
r = client.post("/api/v1/subscriptions", headers=H, json={"price": "4.99", "card_info": "**** 1234"})
assert r.status_code == 201, r.text
print("구독 시작 OK -> 구독 결제 생성")

# 2) 캐릭터 구매 → 결제(character) 생성
r = client.post(f"/api/v1/characters/{ch_id}/purchase", headers=H, json={"card_info": "**** 5678"})
assert r.status_code == 201, r.text
print("캐릭터 구매 OK -> 캐릭터 결제 생성")

# 3) 전체 탭
r = client.get("/api/v1/payments?type=all", headers=H)
assert r.status_code == 200, r.text
page = r.json()
assert len(page["items"]) == 2, page
assert page["month_total"] in ("14.89", 14.89), page["month_total"]  # 4.99 + 9.90
print("전체 탭 OK -> 2건, 이번달 합계", page["month_total"])

# 4) 구독 탭
r = client.get("/api/v1/payments?type=subscribe", headers=H)
items = r.json()["items"]
assert len(items) == 1 and items[0]["category"] == "subscribe" and items[0]["card_info"] == "**** 1234"
print("구독 탭 OK -> 1건, 카드", items[0]["card_info"])

# 5) 캐릭터 탭
r = client.get("/api/v1/payments?type=character", headers=H)
items = r.json()["items"]
assert len(items) == 1 and items[0]["category"] == "character"
print("캐릭터 탭 OK -> 1건, 금액", items[0]["price"])

# 6) 잘못된 type → 422
assert client.get("/api/v1/payments?type=foo", headers=H).status_code == 422
print("잘못된 type 422 OK")

print("\npayment API end-to-end 전부 통과 ✅")
