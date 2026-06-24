"""스모크 공용 — 회원가입 헬퍼.

가입은 이메일 인증 없이 이메일 + 비밀번호만으로 즉시 완료된다.
"""

from __future__ import annotations


def signup(client, email: str, **fields):
    """회원가입한다. signup 응답(Response)을 반환."""
    return client.post("/api/v1/auth/signup", json={"email": email, **fields})
