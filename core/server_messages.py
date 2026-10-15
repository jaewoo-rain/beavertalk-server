"""서버 한국어 고정 문구(오류 메시지 63 · 결제 라벨 3)의 29개 언어 사전 — 2026-10-16 PM-DEC-673.

왜 사전인가: 오류 `detail.message`·WS `error.message` 는 지금 한국어 한 벌이다. 응답 필드를 바꾸면 옛 앱이
깨질 수 있어(필드 추가만 허용), 앱이 이 사전을 받아 «한국어 원문 → 회원 언어» 로 바꿔 보인다.
  · 원문 키 = 응답에 실리는 한국어 그대로(`ko`). f-string 문구는 `{0}`·`{1}` 자리표시 틀이다(원 변수 순서).
  · 결제 라벨은 `payment.category` 코드(subscription·character·bundle)로 고른다 — 서버는 description 을 바꾸지 않는다(R4).
  · 번역이 없는 언어·ko 는 원문을 값으로 둔다(null 금지).
값 원천: 55 커리큘럼데이터 하네스 `10_번역표/server_errors.json`·`r4_payment_label.json`(AI 번역 · 원어민 검수 아님) →
`assets/i18n/server_messages.json`(생성 스크립트 · 손으로 옮기지 않음).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

from core.languages import normalize_locale

_PATH = Path(__file__).resolve().parent.parent / "assets" / "i18n" / "server_messages.json"


@lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads(_PATH.read_text(encoding="utf-8"))


def locales() -> list[str]:
    return list(_data()["locales"])


def bundle(locale: Optional[str]) -> dict:
    """GET /i18n/server-messages 응답 — 한 언어치. 모르는 언어·ko 는 원문."""
    loc = normalize_locale(locale) or "ko"
    d = _data()
    return {
        "locale": loc,
        "label": d["label"],
        "messages": [
            {"key": m["key"], "codes": m["codes"], "ko": m["ko"], "text": m["t"].get(loc) or m["ko"]}
            for m in d["messages"]
        ],
        "payment_labels": {code: v.get(loc) or v["ko"] for code, v in d["payment_labels"].items()},
    }
