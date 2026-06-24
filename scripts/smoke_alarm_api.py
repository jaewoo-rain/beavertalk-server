"""alarm API end-to-end 스모크 (sqlite, Supabase 불필요).

생성(요일 중첩) → 조회 → 요일 전체교체(orphan removal) → 비활성 → 삭제 → 소유 격리.
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

# 시드 + 두 명의 회원
with TestingSessionLocal() as db:
    sc = SpeakCountry(first_country="USA", first_percent=80)
    ch = Character(name="비버", price=Decimal("0"), image_url="b.png")
    db.add_all([sc, ch])
    db.commit()
    sc_id, ch_id = sc.speak_country_id, ch.character_id


from scripts.smoke_signup_helper import signup  # noqa: E402


def signup_login(email: str) -> dict:
    signup(client, email, password="pw", speak_country_id=sc_id, character_id=ch_id)
    tok = client.post("/api/v1/auth/login", data={"username": email, "password": "pw"}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


H = signup_login("alarmer@bt.io")
H2 = signup_login("other@bt.io")

t = datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc).isoformat()

# 1) 생성 — 요일 중첩
r = client.post("/api/v1/alarms", headers=H, json={
    "character_id": ch_id, "time": t, "days_of_week": ["MON", "WED", "FRI"],
})
assert r.status_code == 201, r.text
alarm = r.json()
aid = alarm["alarm_id"]
assert set(alarm["days_of_week"]) == {"MON", "WED", "FRI"}, alarm
assert alarm["character"]["name"] == "비버"
print("생성 OK -> 요일", sorted(alarm["days_of_week"]), "캐릭터", alarm["character"]["name"])

# 1-1) 잘못된 요일 → 422
r_bad = client.post("/api/v1/alarms", headers=H, json={
    "character_id": ch_id, "time": t, "days_of_week": ["FUNDAY"],
})
assert r_bad.status_code == 422, r_bad.text
print("잘못된 요일 422 OK")

# 2) 목록
r = client.get("/api/v1/alarms", headers=H)
assert r.status_code == 200 and len(r.json()) == 1
print("목록 OK -> 1건")

# 3) 요일 전체 교체 (MON,WED,FRI -> TUE,THU) = orphan removal
r = client.put(f"/api/v1/alarms/{aid}", headers=H, json={"days_of_week": ["TUE", "THU"]})
assert r.status_code == 200, r.text
assert set(r.json()["days_of_week"]) == {"TUE", "THU"}, r.json()
print("요일 교체 OK -> 옛 요일 삭제, 새 요일", sorted(r.json()["days_of_week"]))

# 4) 비활성 토글
r = client.post(f"/api/v1/alarms/{aid}/deactivate", headers=H)
assert r.status_code == 200 and r.json()["is_activate"] is False
print("비활성 OK -> is_activate=False")

# 5) 소유 격리 — 다른 회원은 접근 불가(404)
assert client.get(f"/api/v1/alarms/{aid}", headers=H2).status_code == 404
print("타인 알람 접근 404 OK")

# 6) 삭제 → 재조회 404
assert client.delete(f"/api/v1/alarms/{aid}", headers=H).status_code == 204
assert client.get(f"/api/v1/alarms/{aid}", headers=H).status_code == 404
print("삭제 OK -> 재조회 404")

print("\nalarm API end-to-end 전부 통과 ✅")
