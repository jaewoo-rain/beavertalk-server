"""commerce API end-to-end 스모크 (sqlite, Supabase 불필요).

목록(할인가·소유여부) → 구매(트랜잭션) → 중복 409 → 내 소유 목록 → 결제 동시생성 검증.
"""

import sys
from datetime import datetime, timedelta, timezone
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
from domains.commerce.models.discount_event import DiscountEvent

# 테스트 한정: PK INTEGER 로 낮춰 sqlite 자동증가
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

# ── 시드: 억양 + 캐릭터 2개(하나는 활성 할인) ──
now = datetime.now(timezone.utc)
with TestingSessionLocal() as db:
    sc = SpeakCountry(first_country="USA", first_percent=80)
    c1 = Character(name="비버", price=Decimal("9.99"), image_url="b.png")
    c2 = Character(name="여우", price=Decimal("5.00"), image_url="f.png")
    db.add_all([sc, c1, c2])
    db.flush()
    # c1 에 활성 할인(9.99 -> 2.99)
    db.add(DiscountEvent(
        character_id=c1.character_id, discount_price=Decimal("2.99"),
        start_time=now - timedelta(days=1), end_time=now + timedelta(days=1), activate=True,
    ))
    db.commit()
    sc_id, c1_id, c2_id = sc.speak_country_id, c1.character_id, c2.character_id

# ── 회원가입 + 로그인 ──
from scripts.smoke_signup_helper import signup  # noqa: E402

signup(client, "buyer@bt.io", password="pw", speak_country_id=sc_id, character_id=c1_id)
token = client.post("/api/v1/auth/login", data={"username": "buyer@bt.io", "password": "pw"}).json()["access_token"]
H = {"Authorization": f"Bearer {token}"}

# 1) 캐릭터 목록 — 할인가/소유여부
r = client.get("/api/v1/characters", headers=H)
assert r.status_code == 200, r.text
chars = {c["character_id"]: c for c in r.json()}
assert chars[c1_id]["price"] == "9.99" and chars[c1_id]["effective_price"] == "2.99", chars[c1_id]
assert chars[c2_id]["effective_price"] == "5.00", chars[c2_id]
assert chars[c1_id]["is_owned"] is False
print("목록 OK -> 비버 정가 9.99 / 할인가 2.99, 여우 5.00, 소유=False")

# 2) 구매 — 할인가로 결제 생성
r = client.post(f"/api/v1/characters/{c1_id}/purchase", headers=H)
assert r.status_code == 201, r.text
body = r.json()
assert body["member_character"]["character_id"] == c1_id
assert body["payment"]["price"] == "2.99", body["payment"]   # 서버가 할인가로 결제
print("구매 OK -> 결제금액 2.99(할인가), member_character+payment 동시 생성")

# 3) 중복 구매 → 409 ALREADY_OWNED
r = client.post(f"/api/v1/characters/{c1_id}/purchase", headers=H)
assert r.status_code == 409, r.text
assert r.json()["detail"]["code"] == "ALREADY_OWNED", r.json()
print("중복 구매 409 ALREADY_OWNED OK")

# 4) 목록 재조회 — 이제 소유=True
r = client.get("/api/v1/characters", headers=H)
assert {c["character_id"]: c for c in r.json()}[c1_id]["is_owned"] is True
print("재조회 OK -> 비버 소유=True")

# 5) 내 소유 캐릭터
r = client.get("/api/v1/members/me/characters", headers=H)
assert r.status_code == 200 and len(r.json()) == 1 and r.json()[0]["character_id"] == c1_id
print("내 소유 목록 OK ->", r.json()[0]["name"])

# 6) 미인증 차단
assert client.get("/api/v1/characters").status_code == 401
print("미인증 401 OK")

print("\ncommerce API end-to-end 전부 통과 ✅")
