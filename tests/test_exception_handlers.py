"""전역 예외 핸들러 3종 — 표준 에러 바디 통일 검증 (외부 의존 0).

검증:
    - HTTPException(문자열) → {"detail": {"code": "HTTP_4xx", "message": ...}}
    - HTTPException(detail=dict) → 그대로 통과(커스텀 code)
    - 요청 검증 실패(422) → {"detail": {"code": "VALIDATION_ERROR", "message", "errors"}}
    - 미처리 예외 → 500 {"detail": {"code": "INTERNAL_ERROR", "error_ref_id"}}, 내부 메시지 미노출.
"""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.testclient import TestClient

from main import create_app


def _client(raise_server_exceptions: bool = True) -> TestClient:
    app = create_app()

    @app.get("/_t/http_str")
    def _s():
        raise HTTPException(409, "이미 있음")

    @app.get("/_t/http_dict")
    def _d():
        raise HTTPException(409, detail={"code": "CUSTOM_X", "message": "커스텀"})

    @app.get("/_t/boom")
    def _b():
        raise ValueError("kaboom-내부비밀")

    @app.get("/_t/validate")
    def _v(n: int):
        return {"n": n}

    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_http_exception_string_wrapped():
    r = _client().get("/_t/http_str")
    assert r.status_code == 409
    assert r.json() == {"detail": {"code": "HTTP_409", "message": "이미 있음"}}


def test_http_exception_dict_passthrough():
    r = _client().get("/_t/http_dict")
    assert r.status_code == 409
    assert r.json() == {"detail": {"code": "CUSTOM_X", "message": "커스텀"}}


def test_validation_error_unified_format():
    r = _client().get("/_t/validate", params={"n": "abc"})
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["code"] == "VALIDATION_ERROR"
    assert "n" in body["message"]           # 어느 필드가 틀렸는지 메시지에 포함
    assert isinstance(body["errors"], list)  # 필드별 상세도 함께


def test_unhandled_exception_500_with_ref():
    r = _client(raise_server_exceptions=False).get("/_t/boom")
    assert r.status_code == 500
    body = r.json()["detail"]
    assert body["code"] == "INTERNAL_ERROR"
    assert body["error_ref_id"]                 # 추적 ID 발급
    assert "kaboom" not in r.text               # 내부 예외 메시지·비밀이 응답에 새지 않음
