"""인증 확장 스모크 — 소셜 로그인 + 비밀번호 재설정 (sqlite)."""

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

# 테스트 한정: 인증 코드 "1234" 고정 + 실제 메일 발송 차단(모킹).
import domains.account.service.email_verification_service as _evs  # noqa: E402

_evs.generate_code = lambda: "1234"
_evs.send_email = lambda *a, **k: None

# ── 소셜 로그인 ──
# 1) 처음 소셜 로그인 → 자동 가입 + 토큰
r = client.post("/api/v1/auth/social", json={"login_method": "kakao", "token": "kakao-uid-123"})
assert r.status_code == 200, r.text
tok1 = r.json()["access_token"]
print("소셜 최초 → 자동가입+토큰 OK")

# 2) 같은 소셜로 다시 → 같은 회원 로그인(중복 가입 안 함)
me1 = client.get("/api/v1/members/me", headers={"Authorization": f"Bearer {tok1}"}).json()
r = client.post("/api/v1/auth/social", json={"login_method": "kakao", "token": "kakao-uid-123"})
tok2 = r.json()["access_token"]
me2 = client.get("/api/v1/members/me", headers={"Authorization": f"Bearer {tok2}"}).json()
assert me1["member_id"] == me2["member_id"], (me1, me2)
print(f"소셜 재로그인 → 동일 회원(member_id={me1['member_id']}) OK")

# ── 비밀번호 재설정 (4자리 코드) ──
RESET_EMAIL = "reset@bt.io"

# 3) 이메일 인증 → 회원가입
assert client.post("/api/v1/auth/email/send-code", json={"email": RESET_EMAIL}).status_code == 200
assert client.post("/api/v1/auth/email/verify-code",
                   json={"email": RESET_EMAIL, "code": "1234"}).status_code == 200
assert client.post("/api/v1/auth/signup",
                   json={"email": RESET_EMAIL, "password": "oldpw"}).status_code == 201
print("가입(이메일 인증 포함) OK")

# 4) 재설정 요청(이메일 존재) → 200, 없는 이메일도 동일 응답(추측 방지)
r = client.post("/api/v1/auth/password-reset/request", json={"email": RESET_EMAIL})
assert r.status_code == 200, r.text
r2 = client.post("/api/v1/auth/password-reset/request", json={"email": "nobody@bt.io"})
assert r2.status_code == 200 and r2.json() == r.json()
print("재설정 요청 OK -> 존재/부재 응답 동일(추측 방지)")

# 5) 잘못된 코드 → 400
r = client.post("/api/v1/auth/password-reset/confirm",
                json={"email": RESET_EMAIL, "code": "0000", "new_password": "x"})
assert r.status_code == 400, r.text
print("잘못된 코드 400 OK")

# 6) 올바른 코드(1234) → 비밀번호 변경
r = client.post("/api/v1/auth/password-reset/confirm",
                json={"email": RESET_EMAIL, "code": "1234", "new_password": "newpw"})
assert r.status_code == 200, r.text
print("재설정 확정 OK")

# 7) 옛 비번 실패 / 새 비번 성공
assert client.post("/api/v1/auth/login", data={"username": RESET_EMAIL, "password": "oldpw"}).status_code == 401
assert client.post("/api/v1/auth/login", data={"username": RESET_EMAIL, "password": "newpw"}).status_code == 200
print("옛 비번 거부 / 새 비번 로그인 OK")

print("\n인증(소셜+비번재설정) end-to-end 전부 통과 ✅")
