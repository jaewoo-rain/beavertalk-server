"""실제 Supabase 로 가입/로그인/조회 라이브 검증. 끝나면 테스트 회원 삭제.

실행: python scripts/smoke_live.py
(가입은 이메일 인증 없이 즉시 완료된다.)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import main  # 실제 get_db(=Supabase) 사용, 오버라이드 안 함

client = TestClient(main.app)
EMAIL = "livetest@beavertalk.io"

# 이전 잔여 회원 정리(있으면 로그인→탈퇴)
r = client.post("/api/v1/auth/login", data={"username": EMAIL, "password": "pw"})
if r.status_code == 200:
    tok = r.json()["access_token"]
    client.delete("/api/v1/members/me", headers={"Authorization": f"Bearer {tok}"})
    print("정리: 기존 테스트 회원 삭제")

# 1) 가입(이메일+비밀번호만, 인증 없음, 캐릭터는 기본 자동 지정)
r = client.post("/api/v1/auth/signup", json={"email": EMAIL, "password": "pw"})
assert r.status_code == 201, r.text
me = r.json()
assert me["character_id"] is not None, me           # 기본(첫) 캐릭터 자동 지정
assert me["reasons"] == [] and me["name"] is None, me  # 가입 단계엔 비어있음
assert me["onboarding_completed"] is False, me        # 아직 온보딩 전
print(f"가입 OK -> member_id={me['member_id']}, 기본 캐릭터 id={me['character_id']}")

# 2) 로그인
tok = client.post("/api/v1/auth/login", data={"username": EMAIL, "password": "pw"}).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}
print("로그인 OK")

# 2-1) 온보딩 — 이름 + 학습이유 + 언어
r = client.post("/api/v1/members/me/onboarding", headers=H, json={
    "name": "라이브", "language": "en", "reasons": ["travel", "exam"],
})
assert r.status_code == 200, r.text
ob = r.json()
assert ob["name"] == "라이브" and sorted(ob["reasons"]) == ["exam", "travel"], ob
assert ob["onboarding_completed"] is True, ob
print(f"온보딩 OK -> name={ob['name']}, reasons={ob['reasons']}, completed={ob['onboarding_completed']}")

# 3) /me
r = client.get("/api/v1/members/me", headers=H)
assert r.status_code == 200 and r.json()["email"] == EMAIL
print("GET /me OK ->", r.json()["email"])

# 4) 캐릭터 목록(비비 시드 확인)
r = client.get("/api/v1/characters", headers=H)
assert r.status_code == 200, r.text
names = [c["name"] for c in r.json()]
assert "비비" in names, names
print("캐릭터 목록 OK ->", names)

# 5) 내 보유 캐릭터(무료 스타터 자동 보유)
owned = client.get("/api/v1/members/me/characters", headers=H).json()
print("보유 캐릭터 ->", [c["name"] for c in owned])

# 6) 정리 — 테스트 회원 삭제
assert client.delete("/api/v1/members/me", headers=H).status_code == 204
print("정리: 테스트 회원 삭제 완료")

print("\n실제 Supabase 라이브 검증 전부 통과 ✅")
