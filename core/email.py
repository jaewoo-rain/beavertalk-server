"""이메일 발송 (Resend).

`RESEND_API_KEY` + `MAIL_FROM` 가 모두 설정돼 있으면 Resend REST API 로 발송하고,
하나라도 없으면(로컬/미설정) 콘솔 출력으로 폴백한다.

발송 실패는 예외를 전파하지 않는다(로깅만). 비밀번호 재설정 요청처럼 "회원 존재 여부를
노출하지 않기 위해 항상 동일 응답" 해야 하는 흐름이 메일 실패로 깨지지 않게 하기 위함.
"""

from __future__ import annotations

import logging

import httpx

from core.config import settings

logger = logging.getLogger("email")

_RESEND_URL = "https://api.resend.com/emails"


def _fallback(to: str, subject: str, body: str) -> None:
    """키 미설정/로컬용 — 실제 발송 대신 콘솔 출력."""
    logger.info("[EMAIL stub] to=%s subject=%s\n%s", to, subject, body)
    print(f"[EMAIL stub] to={to} | {subject}\n{body}")


def send_email(to: str, subject: str, body: str) -> None:
    """[to]에게 제목 [subject], 본문 [body](plain text) 메일 발송."""
    if not settings.RESEND_API_KEY or not settings.MAIL_FROM:
        _fallback(to, subject, body)
        return

    try:
        resp = httpx.post(
            _RESEND_URL,
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            json={
                "from": settings.MAIL_FROM,
                "to": [to],
                "subject": subject,
                "text": body,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        logger.info("[EMAIL sent] to=%s id=%s", to, resp.json().get("id"))
    except Exception as exc:  # noqa: BLE001  (네트워크/4xx/5xx 모두)
        # 발송 실패가 호출 흐름(가입/재설정 요청)을 막지 않도록 삼킨다.
        logger.error("[EMAIL fail] to=%s subject=%s err=%s", to, subject, exc)
