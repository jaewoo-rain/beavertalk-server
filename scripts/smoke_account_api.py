"""account API end-to-end 스모크 (sqlite, Supabase 불필요).

회원가입 → 로그인(JWT) → /me(보호 라우터) → 미인증 차단 까지 실제 HTTP 흐름 검증.
get_db 를 인메모리 sqlite 로 오버라이드한다.
"""

import sys
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

# 테스트 한정: PK 를 INTEGER 로 낮춰 sqlite rowid 자동증가 활성화.
# (실제 Postgres 에선 BigInteger + Identity() 그대로 동작 — 모델은 안 건드림)
for _table in Base.metadata.tables.values():
    for _pk in _table.primary_key.columns:
        _pk.type = Integer()

# 1) 공유 인메모리 sqlite
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
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

# 2) FK 충족용 시드(speak_country, character)
with TestingSessionLocal() as db:
    sc = SpeakCountry(first_country="USA", first_percent=80)
    ch = Character(name="비버", price=0)
    db.add_all([sc, ch])
    db.commit()
    sc_id, ch_id = sc.speak_country_id, ch.character_id
print(f"seed: speak_country_id={sc_id}, character_id={ch_id}")

# 2-1) 가입 전 이메일 사용가능 → available True
r = client.get("/api/v1/auth/email/available", params={"email": "test@beavertalk.io"})
assert r.status_code == 200 and r.json()["available"] is True
print("이메일 사용가능 확인 OK")

# 3) 회원가입(이메일 + 비밀번호만, 인증 없음)
r = client.post("/api/v1/auth/signup", json={
    "email": "test@beavertalk.io", "password": "hunter2",
})
assert r.status_code == 201, r.text
me = r.json()
assert me["email"] == "test@beavertalk.io"
assert "password" not in me                       # 비번 응답 제외
assert me["reasons"] == [] and me["name"] is None  # 가입 단계엔 비어있음
assert me["onboarding_completed"] is False          # 아직 온보딩 전
print("signup OK (이메일+비번만, onboarding_completed=False) ->", me["member_id"])

# 3-1) 가입 후 이메일 사용가능 → available False
r = client.get("/api/v1/auth/email/available", params={"email": "test@beavertalk.io"})
assert r.status_code == 200 and r.json()["available"] is False
print("가입 후 중복확인 False OK")

# 3-2) 이미 가입 이메일로 재가입 → 409
r_dup = client.post("/api/v1/auth/signup", json={
    "email": "test@beavertalk.io", "password": "hunter2",
})
assert r_dup.status_code == 409, r_dup.text
print("이미 가입 이메일 재가입 409 OK")

# 3-3) character_id 없이 가입 → 첫(무료) 캐릭터가 기본 대표 + 자동 보유
from domains.commerce.models.member_character import MemberCharacter as _MC  # noqa: E402

r = client.post("/api/v1/auth/signup", json={"email": "default@beavertalk.io", "password": "pw"})
assert r.status_code == 201, r.text
md = r.json()
assert md["character_id"] == ch_id, md  # 첫 캐릭터가 기본 대표
with TestingSessionLocal() as _db:
    owned = _db.query(_MC).filter_by(member_id=md["member_id"]).all()
assert len(owned) == 1 and owned[0].character_id == ch_id, owned
print("기본 캐릭터 자동 지정+보유 OK")

# 3-4) 억양(speak_country)은 가입/온보딩 입력이 아니므로 마이페이지 테스트용으로 직접 세팅
from domains.account.models.member import Member as _M  # noqa: E402

with TestingSessionLocal() as _db:
    m = _db.query(_M).filter_by(email="test@beavertalk.io").first()
    m.speak_country_id = sc_id
    _db.commit()

# 4) 로그인(form) → JWT
r = client.post("/api/v1/auth/login", data={
    "username": "test@beavertalk.io", "password": "hunter2",
})
assert r.status_code == 200, r.text
token = r.json()["access_token"]
print("login OK -> token 앞 20자:", token[:20], "...")

# 4-1) 틀린 비번 → 401
r_bad = client.post("/api/v1/auth/login", data={
    "username": "test@beavertalk.io", "password": "wrong",
})
assert r_bad.status_code == 401, r_bad.text
print("틀린 비번 401 OK")

# 5) /me — 토큰 주입
r = client.get("/api/v1/members/me", headers={"Authorization": f"Bearer {token}"})
assert r.status_code == 200, r.text
assert r.json()["email"] == "test@beavertalk.io"
print("GET /me OK ->", r.json()["email"])

# 5-1) 마이페이지 — 억양 전체 + 구독여부
r = client.get("/api/v1/members/me/profile", headers={"Authorization": f"Bearer {token}"})
assert r.status_code == 200, r.text
prof = r.json()
assert prof["is_subscribed"] is False and prof["speak_country"] is not None
print("마이페이지 OK -> 구독", prof["is_subscribed"], ", 억양 1순위", prof["speak_country"]["first_country"])

# 5-2) 온보딩 — 이름 + 학습이유 + 언어 저장(전용 엔드포인트)
AUTH = {"Authorization": f"Bearer {token}"}
r = client.post("/api/v1/members/me/onboarding", headers=AUTH, json={
    "name": "재우", "language": "ko", "reasons": ["travel", "career", "travel"],
})
assert r.status_code == 200, r.text
ob = r.json()
assert ob["name"] == "재우" and ob["language"] == "ko"
assert sorted(ob["reasons"]) == ["career", "travel"], ob["reasons"]  # 중복 제거
assert ob["onboarding_completed"] is True  # 온보딩 완료 플래그
print("온보딩 저장 OK -> name:", ob["name"], ", reasons:", ob["reasons"], ", completed:", ob["onboarding_completed"])

# 5-3) 온보딩 잘못된 학습 이유 → 400
r_bad = client.post("/api/v1/members/me/onboarding", headers=AUTH, json={"reasons": ["unknown"]})
assert r_bad.status_code == 400, r_bad.text
print("온보딩 알 수 없는 학습 이유 400 OK")

# 6) 토큰 없이 /me → 401
r = client.get("/api/v1/members/me")
assert r.status_code == 401, r.text
print("미인증 /me 401 OK")

print("\naccount API end-to-end 전부 통과 ✅")
