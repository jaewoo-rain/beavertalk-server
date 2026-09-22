"""C1 — 구독 2단화(Free/premium) 회귀 (D1, 2026-09-22).

옛 3티어(pro/max)를 판매 전에 전부 premium 하나로 합친다. 이 파일이 지키는 것:
  - 통화 엔진 표(영상·조각·모델) 3개가 Free(None)/premium 두 키만 갖는다.
  - `plan_override` 에 옛 값(pro/max)을 보내면 WS·REST 모두 422 로 거절한다.
  - 코드에 옛 플랜 리터럴("pro"/"max")이 plan 값으로 남지 않는다(스토어 상품 ID
    문자열 `bt_pro_*`·`bt_max_*` 는 예외 — 스토어 등록값이라 이름은 그대로 둔다).
"""

from __future__ import annotations

import pathlib
import re

import pytest
from pydantic import ValidationError

from domains.learning.realtime.protocol import ClientStart
from domains.learning.service import call_service as cs

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_engine_tables_have_only_free_and_premium_keys():
    for table in (cs.CALL_VIDEO_BY_PLAN, cs.CALL_FRAGMENTS_BY_PLAN, cs.CALL_LIVE_MODEL_BY_PLAN):
        assert set(table.keys()) == {None, "premium"}


@pytest.mark.parametrize("stale_plan", ["pro", "max"])
def test_ws_client_start_rejects_the_old_three_tier_values(stale_plan):
    with pytest.raises(ValidationError):
        ClientStart(plan_override=stale_plan)


def test_ws_client_start_accepts_the_new_two_tier_values():
    assert ClientStart(plan_override="free").plan_override == "free"
    assert ClientStart(plan_override="premium").plan_override == "premium"
    assert ClientStart(plan_override=None).plan_override is None


# --------------------------------------------------------------------------- #
# ⛔ pro/max 가 **plan 값**으로 코드에 남지 않는지 — C1 명세(§«현재 코드 사실»)가
#   찍은 지점들만 스캔한다. "max" 는 흔한 낱말(턴 상한·디버그 필드명 등)이라 전체
#   레포 grep 은 오탐이 많다 — 정확히 그 함정을 피한 것이 이 시험의 요점이다.
# --------------------------------------------------------------------------- #
_PLAN_LITERAL = re.compile(r"""(['"])(pro|max)\1""")
_ALLOWED_SUBSTRINGS = ("bt_pro_", "bt_max_")  # 스토어 상품 id — 이름은 그대로 둔다(iap_catalog 계약)

_SCANNED_FILES = (
    "domains/commerce/models/subscribe.py",
    "domains/commerce/schemas/subscription.py",
    "domains/commerce/service/iap_catalog.py",
    "domains/commerce/service/subscription_status.py",
    "domains/commerce/service/entitlements.py",
    "domains/commerce/service/iap_service.py",
    "domains/learning/realtime/protocol.py",
    "domains/learning/routers/call.py",
    "domains/learning/service/call_service.py",
    "main.py",
)


def test_no_bare_pro_max_plan_literals_in_the_plan_carrying_files():
    offenders: list[str] = []
    for rel in _SCANNED_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for m in _PLAN_LITERAL.finditer(text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            line = text[line_start:line_end if line_end != -1 else None]
            if any(s in line for s in _ALLOWED_SUBSTRINGS):
                continue
            offenders.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}: {line.strip()}")
    assert not offenders, "옛 pro/max 플랜 리터럴이 남아 있다:\n" + "\n".join(offenders)
