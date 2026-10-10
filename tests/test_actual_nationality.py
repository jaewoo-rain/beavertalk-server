"""실제 국적(member.actual_nationality) · 국적 판정 요청 수집 필드 — 2026-10-10 PM-DEC-495·498·499·502·505.

- 저장: ISO 3166-1 alpha-2(국가 표 249개 · 웹과 같은 표) · 표 밖이면 422 · PATCH null 은 「변경 없음」.
- 전송: 통화 끝 /predict_long **같은 요청 1회**에 client 필드 + actual_nationality(영문명).
  스위치 NATIONALITY_SEND_ACTUAL(기본 True) · NATIONALITY_SEND_CLIENT_INFO(기본 True).
- 응답 헤더 X-Capture-ID 는 로그로 남긴다.
"""
from __future__ import annotations

import logging

import pytest
from fastapi import HTTPException

import core.nationality as natl
from core import client_info
from core.config import Settings
from domains.account.schemas.member import MemberRead, MemberUpdate, MyPageOut, OnboardingIn
from domains.learning.realtime import call_session as cs


# --------------------------------------------------------------------------- #
# 1) 국가 표 · 수집 필드 정리
# --------------------------------------------------------------------------- #
def test_country_table_uses_model_label_spellings():
    """모델 라벨 41개는 모델 표기 그대로 — 이름이 갈리면 모델팀 집계가 나라를 둘로 센다."""
    for label, iso in natl._LABEL_ISO.items():
        assert client_info.country_name(iso) == label
    assert client_info.country_name("ph") == "Philippines"
    assert client_info.country_name("NO") == "Norway"  # 41개 밖 국가도 저장·전송 가능(모델팀 3차)
    assert client_info.country_name("XX") is None
    assert client_info.country_name(None) is None
    assert len(client_info._en_by_iso()) == 249


def test_client_form_is_app_and_drops_empty_and_cleans():
    form = client_info.client_form({
        "client_session": "3f2a-uuid", "os": "android", "os_version": "14",
        "app_version": "1.0.1+55", "device_type": "phone", "token": "secret",
    })
    assert form == {
        "client_type": "app", "session_id": "3f2a-uuid", "os": "android",
        "os_version": "14", "app_version": "1.0.1+55", "device_type": "phone",
    }
    assert "token" not in form  # 소켓 쿼리의 토큰은 절대 싣지 않는다
    assert client_info.client_form({"os": "  ", "device_type": "<tablet>"}) == {
        "client_type": "app", "device_type": "tablet",
    }
    assert client_info.client_form(None) == {"client_type": "app"}


def test_switch_defaults_are_on():
    """PM-DEC-505(전송 켬) · PM-DEC-499(client 필드 켬). 되돌리기는 env false."""
    fields = Settings.model_fields
    assert fields["NATIONALITY_SEND_ACTUAL"].default is True
    assert fields["NATIONALITY_SEND_CLIENT_INFO"].default is True


# --------------------------------------------------------------------------- #
# 2) 통화 끝 요청에 실을 필드 — 스위치 두 개
# --------------------------------------------------------------------------- #
class _Db:
    def __init__(self, iso):
        self.iso = iso

    def scalar(self, _stmt):
        return self.iso


_CLIENT = {"client_type": "app", "session_id": "s1", "os": "ios"}


def test_fields_carry_client_and_actual_name(monkeypatch):
    monkeypatch.setattr(cs._settings, "NATIONALITY_SEND_ACTUAL", True, raising=False)
    monkeypatch.setattr(cs._settings, "NATIONALITY_SEND_CLIENT_INFO", True, raising=False)
    assert cs._nationality_fields(_Db("RU"), 1, _CLIENT) == {**_CLIENT, "actual_nationality": "Russia"}


def test_fields_omit_actual_when_not_chosen(monkeypatch):
    monkeypatch.setattr(cs._settings, "NATIONALITY_SEND_ACTUAL", True, raising=False)
    monkeypatch.setattr(cs._settings, "NATIONALITY_SEND_CLIENT_INFO", True, raising=False)
    assert cs._nationality_fields(_Db(None), 1, _CLIENT) == _CLIENT


def test_switches_off_drop_each_part(monkeypatch):
    monkeypatch.setattr(cs._settings, "NATIONALITY_SEND_ACTUAL", False, raising=False)
    monkeypatch.setattr(cs._settings, "NATIONALITY_SEND_CLIENT_INFO", True, raising=False)
    assert cs._nationality_fields(_Db("RU"), 1, _CLIENT) == _CLIENT

    monkeypatch.setattr(cs._settings, "NATIONALITY_SEND_ACTUAL", True, raising=False)
    monkeypatch.setattr(cs._settings, "NATIONALITY_SEND_CLIENT_INFO", False, raising=False)
    assert cs._nationality_fields(_Db("RU"), 1, _CLIENT) == {"actual_nationality": "Russia"}


# --------------------------------------------------------------------------- #
# 3) /predict_long 요청 — 같은 요청 1회에 필드 · X-Capture-ID 로그
# --------------------------------------------------------------------------- #
class _Resp:
    status_code = 200
    headers = {"X-Capture-ID": "cap-123"}

    def json(self):
        return {"top5": [{"label": "Korea", "prob": 0.9}], "sec": 12.0, "total_ms": 80}

    def raise_for_status(self):
        return None


def _client_factory(calls):
    class _C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, files=None, headers=None, **kwargs):
            calls.append(kwargs)
            return _Resp()

    return _C


def test_predict_sends_fields_once_and_logs_capture_id(monkeypatch, caplog):
    monkeypatch.setattr(natl.settings, "NATIONALITY_API_URL", "http://nat.example", raising=False)
    monkeypatch.setattr(natl.settings, "NATIONALITY_API_KEY", None, raising=False)
    calls: list = []
    monkeypatch.setattr(natl.httpx, "Client", _client_factory(calls))
    with caplog.at_level(logging.INFO, logger=natl.logger.name):
        out = natl.predict_nationality(
            b"pcm", "wav", {**_CLIENT, "actual_nationality": "Russia", "os_version": ""},
        )
    assert out is not None
    assert len(calls) == 1  # 따로 재전송하지 않는다(PM-DEC-498)
    assert calls[0]["data"] == {"parts": "1", **_CLIENT, "actual_nationality": "Russia"}
    assert "capture_id=cap-123" in caplog.text
    assert "Russia" not in caplog.text  # 국가명은 로그에 남기지 않는다


def test_predict_without_fields_is_unchanged(monkeypatch):
    monkeypatch.setattr(natl.settings, "NATIONALITY_API_URL", "http://nat.example", raising=False)
    calls: list = []
    monkeypatch.setattr(natl.httpx, "Client", _client_factory(calls))
    natl.predict_nationality(b"pcm")
    assert calls[0]["data"] == {"parts": "1"}


# --------------------------------------------------------------------------- #
# 4) 저장 — 온보딩 · PATCH · DTO
# --------------------------------------------------------------------------- #
class _FakeMember:
    def __init__(self) -> None:
        self.language = None
        self.target_language = None
        self.actual_nationality = None
        self.name = None
        self.onboarding_completed = False
        self.reasons: list = []


class _FakeDb:
    def commit(self) -> None: ...
    def refresh(self, _obj) -> None: ...


def _service_with(member):
    from domains.account.service.member_service import MemberService

    svc = MemberService.__new__(MemberService)
    svc.db = _FakeDb()
    svc.get = lambda _mid: member  # type: ignore[method-assign]
    return svc


def test_dtos_expose_actual_nationality():
    for model in (MemberRead, MemberUpdate, MyPageOut, OnboardingIn):
        assert "actual_nationality" in model.model_fields, model.__name__


def test_onboarding_saves_upper_iso_and_rejects_unknown():
    m = _FakeMember()
    _service_with(m).onboarding(1, "Ana", None, "es", actual_nationality="ph")
    assert m.actual_nationality == "PH"
    assert m.language == "es"  # 모국어는 따로 — 섞이지 않는다

    with pytest.raises(HTTPException) as e:
        _service_with(_FakeMember()).onboarding(1, None, None, None, actual_nationality="ZZ")
    assert e.value.status_code == 422


def test_onboarding_without_nationality_keeps_old_value():
    m = _FakeMember()
    m.actual_nationality = "VN"
    _service_with(m).onboarding(1, "Ana", None, None)
    assert m.actual_nationality == "VN"


def test_patch_changes_country_but_null_is_no_change():
    """앱에서 국적을 지우는 길은 없다(PM-DEC-502) — null 은 「변경 없음」."""
    m = _FakeMember()
    m.actual_nationality = "VN"
    svc = _service_with(m)
    svc.update(1, MemberUpdate(actual_nationality="jp"))
    assert m.actual_nationality == "JP"
    svc.update(1, MemberUpdate(actual_nationality=None))
    assert m.actual_nationality == "JP"
    with pytest.raises(HTTPException):
        svc.update(1, MemberUpdate(actual_nationality="Japan"))
