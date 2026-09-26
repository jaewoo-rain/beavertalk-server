"""S2 선행(2026-09-26, Play 심사 대비) — delete_auth_user 의 「이미 없음」 멱등화.

배경: member_service.delete() 는 delete_auth_user() 성공 후에 로컬 commit 을 한다.
그 사이(auth 는 지워졌는데 커밋 전)에 프로세스가 죽으면, 재시도 시 이미 없는 uid 를
다시 지우려다 Supabase 가 404(AuthApiError)를 던진다. 예전엔 그 예외를 "진짜 실패"와
구분 없이 묻어 False 를 돌려줘 **재시도가 영구히 502 로 실패**했다 — 이제 404/
user_not_found 는 "이미 목표를 이뤘다"로 보고 True 를 돌린다.
"""

from __future__ import annotations

import pytest
from supabase_auth.errors import AuthApiError, AuthRetryableError

from core import supabase_auth, supabase_client


class _FakeAdmin:
    def __init__(self, raise_exc: Exception | None):
        self._raise_exc = raise_exc
        self.calls: list[str] = []

    def delete_user(self, uid: str) -> None:
        self.calls.append(uid)
        if self._raise_exc is not None:
            raise self._raise_exc


class _FakeAuth:
    def __init__(self, admin: _FakeAdmin):
        self.admin = admin


class _FakeClient:
    def __init__(self, admin: _FakeAdmin):
        self.auth = _FakeAuth(admin)


def _patch_client(monkeypatch, raise_exc: Exception | None) -> _FakeAdmin:
    admin = _FakeAdmin(raise_exc)
    monkeypatch.setattr(supabase_client, "get_client", lambda: _FakeClient(admin))
    return admin


def test_delete_succeeds_normally(monkeypatch):
    admin = _patch_client(monkeypatch, None)
    assert supabase_auth.delete_auth_user("uid-1") is True
    assert admin.calls == ["uid-1"]


def test_delete_already_gone_404_is_treated_as_success(monkeypatch):
    """⛔⛔ 핵심 회귀 — 재시도가 이미 없는 uid 를 만나도 성공으로 접어야 멱등하다."""
    exc = AuthApiError("User not found", 404, "user_not_found")
    _patch_client(monkeypatch, exc)
    assert supabase_auth.delete_auth_user("uid-gone") is True


def test_delete_404_status_without_matching_code_is_still_success(monkeypatch):
    """code 가 없거나 다르더라도 status==404 면 「없음」으로 본다(응답 형태 드리프트 대비)."""
    exc = AuthApiError("Not found", 404, None)
    _patch_client(monkeypatch, exc)
    assert supabase_auth.delete_auth_user("uid-gone-2") is True


def test_delete_real_failure_status_is_still_false(monkeypatch):
    """404 가 아닌 AuthApiError(권한·서버 오류 등)는 여전히 실패로 남는다."""
    exc = AuthApiError("forbidden", 403, "not_admin")
    _patch_client(monkeypatch, exc)
    assert supabase_auth.delete_auth_user("uid-forbidden") is False


def test_delete_network_error_is_still_false(monkeypatch):
    """AuthApiError 가 아닌 예외(네트워크 등)는 그대로 실패 처리한다(기존 동작 보존)."""
    _patch_client(monkeypatch, AuthRetryableError("network blip", 503))
    assert supabase_auth.delete_auth_user("uid-net") is False


def test_delete_no_client_is_false(monkeypatch):
    monkeypatch.setattr(supabase_client, "get_client", lambda: None)
    assert supabase_auth.delete_auth_user("uid-x") is False


def test_delete_empty_uid_is_false(monkeypatch):
    assert supabase_auth.delete_auth_user("") is False
