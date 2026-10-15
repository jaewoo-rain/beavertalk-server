"""i18n 라우터 — 서버 고정 문구 사전(GET /i18n/server-messages · 2026-10-16 PM-DEC-673).

인증 없음 — 정적 문구라 개인 정보가 없고, 로그인 전 화면(가입 오류)에서도 쓴다. 언어는 쿼리로 받는다
(member.language 를 모르는 시점이 있다). 사전 구성은 core/server_messages.py.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from core import server_messages

router = APIRouter(prefix="/i18n", tags=["i18n"])


@router.get("/server-messages")
def get_server_messages(
    locale: Optional[str] = Query(None, description="ISO 639-1(예: vi). 없거나 모르면 한국어 원문"),
) -> dict:
    """{locale, label, messages:[{key, codes, ko, text}], payment_labels:{subscription|character|bundle: text}}."""
    return server_messages.bundle(locale)
