"""복습 발음 채점 end-to-end 스모크 (sqlite)."""

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
    db.add(Character(name="비비", price=Decimal("0")))
    db.commit()
    ch_id = 1

from scripts.smoke_signup_helper import signup  # noqa: E402

signup(client, "rev@bt.io", password="pw")
tok = client.post("/api/v1/auth/login", data={"username": "rev@bt.io", "password": "pw"}).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}

# 통화 저장(발화 "안녕하세요")
payload = {
    "character_id": ch_id, "call_date": datetime.now(timezone.utc).isoformat(),
    "sentences": [{"korean_sentence": "안녕하세요", "native_sentence": "Hello", "locale": "en",
                   "evaluation": {"total_score": 80}}],
}
detail = client.post("/api/v1/calls", headers=H, json=payload).json()
sid = detail["sentences"][0]["sentence_id"]

# 1) 복습 제출(녹음) → 발음 채점 피드백 반환
r = client.post(f"/api/v1/sentences/{sid}/reviews", headers=H, json={"voice_url": "reviews/1.opus"})
assert r.status_code == 201, r.text
fb = r.json()
assert fb["korean_sentence"] == "안녕하세요"
assert fb["native_sentence"] == "Hello"
assert len(fb["char_scores"]) == 5  # 안 녕 하 세 요
assert {c["grade"] for c in fb["char_scores"]} <= {"상", "중", "하"}
assert set(fb["evaluation"].keys()) == {"total_score", "pronunciation", "fluency", "rhythm"}
rid = fb["review_id"]
print("복습 채점 OK -> 글자수", len(fb["char_scores"]), ", 첫글자",
      fb["char_scores"][0]["char"], fb["char_scores"][0]["grade"],
      ", 총점", fb["evaluation"]["total_score"])

# 2) 피드백 페이지 GET
r = client.get(f"/api/v1/reviews/{rid}/feedback", headers=H)
assert r.status_code == 200 and r.json()["review_id"] == rid
print("피드백 페이지 GET OK")

# 3) 타인 접근 404
signup(client, "x@bt.io", password="pw")
tok2 = client.post("/api/v1/auth/login", data={"username": "x@bt.io", "password": "pw"}).json()["access_token"]
assert client.get(f"/api/v1/reviews/{rid}/feedback", headers={"Authorization": f"Bearer {tok2}"}).status_code == 404
print("타인 피드백 접근 404 OK")

print("\n복습 발음채점 end-to-end 전부 통과 ✅")
