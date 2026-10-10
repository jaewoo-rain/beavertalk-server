"""국적 분류 서버(/predict_long)에 함께 보내는 수집 필드 — 앱용(2026-10-10 · PM-DEC-495·498·499).

모델팀 계약(3차 회신)과 같은 이름을 쓴다. 웹(beavertalkweb `services/accent/client_info.py`)과 규칙이 같다.

- client_type: 앱은 언제나 "app"(서버가 붙인다 — 앱이 보내는 값이 아니다).
- session_id: 같은 사용자의 반복 시도를 묶는 익명 id. 앱 설치당 UUID 하나를 통화 소켓
  쿼리 `client_session` 으로 보낸다.
- os · os_version · app_version · device_type: 앱이 소켓 쿼리로 보낸 값. 모르는 값은 앱이 빼고,
  여기서도 빈 값은 싣지 않는다(「빈 값·추측값 금지」).
- actual_nationality: 회원이 고른 실제 국적(member.actual_nationality · ISO)의 **영문 국가명**.

앱이 보낸 값은 위조할 수 있다 — 집계 참고값일 뿐 판정·과금에 쓰지 않는다. 문자·길이만 잘라 넘긴다.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Optional

# 소켓 쿼리 이름 → NPU 로 보내는 이름
_FIELDS = {
    "client_session": "session_id",
    "os": "os",
    "os_version": "os_version",
    "app_version": "app_version",
    "device_type": "device_type",
}
_MAX_LEN = 64
# 웹 규칙에 `+` 를 더했다 — Flutter 버전 「1.0.1+55」 의 빌드 번호를 지우면 「1.0.155」 라는 없는 버전이 된다.
_ALLOWED = re.compile(r"[^0-9A-Za-z ._+\-]")


def clean(value: Optional[str]) -> Optional[str]:
    """허용 문자(영숫자·공백·. _ + -)만 남기고 64자로 자른다. 비면 None."""
    if value is None:
        return None
    v = _ALLOWED.sub("", str(value)).strip()[:_MAX_LEN]
    return v or None


def client_form(raw: Optional[Mapping[str, Optional[str]]]) -> dict[str, str]:
    """소켓 쿼리 값 → NPU multipart 필드(client_type 포함). 빈 값은 싣지 않는다."""
    form = {"client_type": "app"}
    for src, dst in _FIELDS.items():
        v = clean((raw or {}).get(src))
        if v:
            form[dst] = v
    return form


@lru_cache(maxsize=1)
def _en_by_iso() -> dict[str, str]:
    """ISO → 영문 국가명 249개. 웹과 같은 표(`assets/countries.json`) · 모델 라벨 41개는 모델 표기."""
    path = Path(__file__).resolve().parents[1] / "assets" / "countries.json"
    return json.loads(path.read_text(encoding="utf-8"))["en_by_iso"]


def normalize_iso(iso: Optional[str]) -> Optional[str]:
    """표에 있는 ISO 3166-1 alpha-2 면 대문자로, 아니면 None."""
    if not iso:
        return None
    code = str(iso).strip().upper()
    return code if code in _en_by_iso() else None


def country_name(iso: Optional[str]) -> Optional[str]:
    """ISO → actual_nationality 값(영문 국가명). 표 밖이면 None."""
    code = normalize_iso(iso)
    return _en_by_iso()[code] if code else None
