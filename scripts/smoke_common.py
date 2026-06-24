"""공통 토대 스모크 — DB 연결 없이 가능한 검증.

- security: 해시 round-trip, JWT 발급/검증
- deps/schemas import
- FastAPI app 부팅 + /health 호출 (TestClient, DB 불필요)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.security import create_access_token, decode_token, hash_password, verify_password

# 1) 비밀번호 해시 round-trip
h = hash_password("hunter2")
assert verify_password("hunter2", h), "올바른 비번 검증 실패"
assert not verify_password("wrong", h), "틀린 비번이 통과됨"
print("password hash/verify OK")

# 2) JWT 발급/검증
tok = create_access_token(123)
payload = decode_token(tok)
assert payload["sub"] == "123", payload
print("JWT encode/decode OK  sub =", payload["sub"])

# 3) 공통 스키마 제네릭
from core.schemas import ErrorResponse, Page  # noqa: E402

p = Page[int](items=[1, 2, 3], limit=20, offset=0, has_more=False)
print("Page[int] OK ->", p.model_dump())

# 4) FastAPI 앱 부팅 + /health (DB 미접속)
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)
r = client.get("/health")
assert r.status_code == 200, r.text
print("/health OK ->", r.json())
print("\n공통 토대 스모크 전부 통과 ✅")
