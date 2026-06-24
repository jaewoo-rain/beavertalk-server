"""learning API end-to-end 스모크 (sqlite, Supabase 불필요).

통화 일괄 저장(call+발화+평가+원본) → 목록 → 상세(중첩) → 원본 → 평점
→ 북마크 → 내 북마크 → 복습 추가/조회 → 삭제 → 소유 격리.
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
    sc = SpeakCountry(first_country="USA", first_percent=80)
    ch = Character(name="비버", price=Decimal("0"), image_url="b.png")
    db.add_all([sc, ch])
    db.commit()
    sc_id, ch_id = sc.speak_country_id, ch.character_id


from scripts.smoke_signup_helper import signup  # noqa: E402


def signup_login(email):
    signup(client, email, password="pw", speak_country_id=sc_id, character_id=ch_id)
    tok = client.post("/api/v1/auth/login", data={"username": email, "password": "pw"}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


H = signup_login("learner@bt.io")
H2 = signup_login("other@bt.io")

# 1) 통화 일괄 저장(발화 2개 + 발화별 평가 + 원본 1개)
payload = {
    "character_id": ch_id,
    "call_date": datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc).isoformat(),
    "total_time": 180, "summary": "첫 통화", "rating": 2,
    "sentences": [
        {"korean_sentence": "안녕하세요", "native_sentence": "Hello", "locale": "en",
         "is_bookmarked": False, "evaluation": {"total_score": 90, "pronunciation": 88, "fluency": 92, "rhythm": 90}},
        {"korean_sentence": "반갑습니다", "native_sentence": "Nice to meet you", "locale": "en",
         "is_bookmarked": False, "evaluation": {}},  # placeholder(점수 NULL)
    ],
    "raw_data": [{"content": "전체 전사...", "voice_url": "calls/1/raw.opus", "total_time": 180}],
}
r = client.post("/api/v1/calls", headers=H, json=payload)
assert r.status_code == 201, r.text
detail = r.json()
cid = detail["call_id"]
assert len(detail["sentences"]) == 2
assert detail["sentences"][0]["evaluation"]["total_score"] == 90
assert detail["sentences"][1]["evaluation"]["total_score"] is None  # placeholder
sid1 = detail["sentences"][0]["sentence_id"]
print("통화 저장 OK -> call+발화2+평가2+원본1, 발화별 1:1 평가(첫 90, 둘째 placeholder)")

# 2) 목록(요약, 발화 미포함)
r = client.get("/api/v1/calls", headers=H)
assert r.status_code == 200 and len(r.json()) == 1
assert "sentences" not in r.json()[0]  # 목록은 얕게
print("목록 OK -> 요약(발화 미포함), 캐릭터", r.json()[0]["character"]["name"])

# 3) 상세(중첩 selectinload+joinedload)
r = client.get(f"/api/v1/calls/{cid}", headers=H)
assert r.status_code == 200 and len(r.json()["sentences"]) == 2
print("상세 OK -> 발화+평가 중첩 로딩")

# 3-1) 통화 결과(평균 + 문장 전체)
r = client.get(f"/api/v1/calls/{cid}/result", headers=H)
assert r.status_code == 200, r.text
res = r.json()
assert res["average"]["total_score"] == 90.0, res["average"]  # [90, None] -> 90
assert len(res["sentences"]) == 2 and "korean_sentence" in res["sentences"][0]
print("결과 OK -> 평균 total_score", res["average"]["total_score"], ", 문장", len(res["sentences"]), "개")

# 4) 원본
r = client.get(f"/api/v1/calls/{cid}/raw", headers=H)
assert r.status_code == 200 and r.json()[0]["voice_url"] == "calls/1/raw.opus"
print("원본 OK")

# 5) 평점 수정
r = client.patch(f"/api/v1/calls/{cid}", headers=H, json={"rating": 3})
assert r.status_code == 200 and r.json()["rating"] == 3
print("평점 OK -> 3")

# 6) 북마크 ON
r = client.patch(f"/api/v1/sentences/{sid1}/bookmark", headers=H, json={"is_bookmarked": True})
assert r.status_code == 200 and r.json()["is_bookmarked"] is True
print("북마크 OK")

# 7) 내 북마크 모음
r = client.get("/api/v1/members/me/bookmarks", headers=H)
assert r.status_code == 200 and len(r.json()) == 1 and r.json()[0]["sentence_id"] == sid1
print("북마크 모음 OK -> 1건")

# 8) 복습 추가 + 조회
r = client.post(f"/api/v1/sentences/{sid1}/reviews", headers=H, json={"voice_url": "reviews/1.opus"})
assert r.status_code == 201, r.text
r = client.get(f"/api/v1/sentences/{sid1}/reviews", headers=H)
assert r.status_code == 200 and len(r.json()) == 1
print("복습 추가/조회 OK -> 1건")

# 8-1) 문장 소프트 삭제 → 상세/결과에서 제외
before = len(client.get(f"/api/v1/calls/{cid}", headers=H).json()["sentences"])
assert client.delete(f"/api/v1/sentences/{sid1}", headers=H).status_code == 204
after = client.get(f"/api/v1/calls/{cid}", headers=H).json()["sentences"]
assert len(after) == before - 1, (before, len(after))
assert all(s["sentence_id"] != sid1 for s in after)
# 소프트 삭제된 문장 북마크 시도 → 404
assert client.patch(f"/api/v1/sentences/{sid1}/bookmark", headers=H, json={"is_bookmarked": True}).status_code == 404
print(f"소프트 삭제 OK -> 문장 {before}→{len(after)}, 삭제문장 북마크 404")

# 9) 소유 격리 — 타인은 상세/북마크 불가
assert client.get(f"/api/v1/calls/{cid}", headers=H2).status_code == 404
assert client.patch(f"/api/v1/sentences/{sid1}/bookmark", headers=H2, json={"is_bookmarked": True}).status_code == 404
print("소유 격리 404 OK")

# 10) 삭제 → 재조회 404 (CASCADE)
assert client.delete(f"/api/v1/calls/{cid}", headers=H).status_code == 204
assert client.get(f"/api/v1/calls/{cid}", headers=H).status_code == 404
print("삭제 OK -> CASCADE, 재조회 404")

print("\nlearning API end-to-end 전부 통과 ✅")
