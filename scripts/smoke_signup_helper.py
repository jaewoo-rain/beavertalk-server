"""스모크 공용 — 이메일 인증 우회 + 가입 헬퍼.

import 시점에 인증 코드를 "1234" 로 고정하고 실제 메일 발송을 차단(모킹)한다.
[signup] 은 send-code → verify-code → signup 3단계를 한 번에 수행한다.
"""

from __future__ import annotations

import domains.account.service.email_verification_service as _evs

_evs.generate_code = lambda: "1234"
_evs.send_email = lambda *a, **k: None


def signup(client, email: str, **fields):
    """이메일 인증을 마치고 회원가입한다. signup 응답(Response)을 반환."""
    client.post("/api/v1/auth/email/send-code", json={"email": email})
    client.post(
        "/api/v1/auth/email/verify-code", json={"email": email, "code": "1234"}
    )
    return client.post("/api/v1/auth/signup", json={"email": email, **fields})
