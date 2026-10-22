"""PM-DEC-997 — 로그 토큰 가리기 · WS 토큰 헤더(Sec-WebSocket-Protocol) 수용."""
from __future__ import annotations

import logging

from core import log_redact
from domains.learning.realtime import ws_router

FAKE = "eyJhbGciOiJIUzI1NiJ9.e30.sig-part_1"


def test_redact_query_token():
    s = log_redact.redact('169.254.1.1:5 - "WebSocket /api/v1/calls/stream?token=%s&os=android" [accepted]' % FAKE)
    assert FAKE not in s and "token=***" in s and "os=android" in s


def test_filter_masks_uvicorn_style_record(caplog):
    log_redact.install()
    lg = logging.getLogger("uvicorn.error")
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        lg.info('%s - "WebSocket %s" [accepted]', "169.254.1.1:5", "/api/v1/calls/stream?token=" + FAKE)
    assert caplog.records
    assert all(FAKE not in r.getMessage() for r in caplog.records)
    assert any("token=***" in r.getMessage() for r in caplog.records)


def test_filter_leaves_other_lines_and_installs_once():
    log_redact.install()
    log_redact.install()
    assert logging.getLogger("uvicorn.access").filters.count(log_redact._FILTER) == 1
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s ok", ("/health",), None)
    assert log_redact._FILTER.filter(rec) and rec.getMessage() == "/health ok"


class _WS:
    def __init__(self, query=None, protos=None):
        self.query_params = query or {}
        self.scope = {"subprotocols": protos or []}


def test_ws_token_from_query_kept_for_old_apps():
    assert ws_router._ws_auth_token(_WS({"token": FAKE})) == (FAKE, None)


def test_ws_token_from_subprotocol():
    assert ws_router._ws_auth_token(_WS(protos=["bt.auth", FAKE])) == (FAKE, "bt.auth")


def test_ws_no_token():
    assert ws_router._ws_auth_token(_WS()) == ("", None)
    assert ws_router._ws_auth_token(_WS(protos=["other", "x"])) == ("", None)
