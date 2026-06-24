"""core.email 발송 로직 단위 테스트 (httpx.post 를 모킹 — 실제 메일 안 보냄)."""

from __future__ import annotations

import core.email as email
from core.config import settings


class _FakeResp:
    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status_code = status
        self._body = body or {"id": "email_123"}

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_sends_via_resend_with_correct_payload(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(settings, "MAIL_FROM", "onboarding@resend.dev")

    captured: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return _FakeResp()

    monkeypatch.setattr(email.httpx, "post", fake_post)

    email.send_email("a@b.com", "제목", "본문")

    assert captured["url"] == email._RESEND_URL
    assert captured["headers"]["Authorization"] == "Bearer re_test_key"
    assert captured["json"] == {
        "from": "onboarding@resend.dev",
        "to": ["a@b.com"],
        "subject": "제목",
        "text": "본문",
    }


def test_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(settings, "MAIL_FROM", "onboarding@resend.dev")

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(email.httpx, "post", boom)

    # 예외가 전파되지 않아야 한다(호출 흐름 보호).
    email.send_email("a@b.com", "제목", "본문")


def test_fallback_when_no_key(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", None)
    monkeypatch.setattr(settings, "MAIL_FROM", "onboarding@resend.dev")

    called = {"n": 0}

    def fake_post(*a, **k):
        called["n"] += 1
        return _FakeResp()

    monkeypatch.setattr(email.httpx, "post", fake_post)

    email.send_email("a@b.com", "제목", "본문")
    assert called["n"] == 0  # 키 없으면 Resend 호출 안 하고 콘솔 폴백
