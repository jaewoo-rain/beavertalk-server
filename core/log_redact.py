"""로그에 실리는 인증 토큰 가리기(PM-DEC-997 · 2026-10-22).

WS 는 인증 토큰을 쿼리(?token=)로 받는다. uvicorn 이 그 경로를 접근 로그에 그대로 찍어
(«INFO: 169.254.x - "WebSocket /api/v1/calls/stream?token=…" [accepted]») 로그 열람 권한만 있으면
토큰이 만료 전까지 재사용될 수 있었다. 여기서는 **우리 컨테이너가 남기는 줄**만 가린다 —
Cloud Run 플랫폼 요청 로그(httpRequest.requestUrl)는 앱이 손댈 수 없다.
"""
from __future__ import annotations

import logging
import re

_TOKEN_RE = re.compile(r"((?:^|[?&])(?:token|access_token)=)[^&\s\"']+")
_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def redact(text: str) -> str:
    """문자열 안의 token=… 값을 *** 로 바꾼다."""
    return _TOKEN_RE.sub(r"\1***", text)


class TokenRedactFilter(logging.Filter):
    """기록을 한 번 포맷해 토큰을 가린 뒤 그 결과를 메시지로 고정한다(args 는 비운다)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — 포맷이 깨진 기록은 그대로 둔다(로그를 잃지 않는다)
            return True
        if "token=" in msg:
            record.msg = redact(msg)
            record.args = None
        return True


_FILTER = TokenRedactFilter()


def install() -> None:
    """uvicorn 로거와 그 핸들러에 필터를 단다(여러 번 불러도 한 번만 붙는다).

    uvicorn CLI 는 앱을 import 하기 전에 로깅을 구성하므로 import 시점에 핸들러가 이미 있다.
    로거와 핸들러 양쪽에 다는 이유: 접근 로그는 uvicorn.access 가 직접 찍지만, 다른 경로로
    전파돼 들어오는 기록도 핸들러에서 한 번 더 걸러진다.
    """
    for name in _LOGGERS:
        lg = logging.getLogger(name)
        if _FILTER not in lg.filters:
            lg.addFilter(_FILTER)
        for h in lg.handlers:
            if _FILTER not in h.filters:
                h.addFilter(_FILTER)
