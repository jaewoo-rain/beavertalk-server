"""한정 할인 — 목록 응답에 active_discount 노출 회귀.

상세 화면은 목록에서 카드를 눌러 진입하고 추가 조회를 하지 않는다(N+1 회피). 그래서
카운트다운 마감 시각(end_time)이 **목록 응답에** 있어야 앱이 그릴 수 있다.
근거: docs/20260729_0453_한정할인-카운트다운과-할인이벤트-운영도구.md
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from domains.commerce.schemas.character import CharacterDetail, CharacterSummary
from domains.commerce.service.character_service import CharacterService


class _Ev:
    def __init__(self, price, start, end, activate=True):
        self.discount_price = price
        self.start_time = start
        self.end_time = end
        self.activate = activate


class _Char:
    def __init__(self, events):
        self.character_id = 2
        self.name = "BIBI"
        self.image_url = None
        self.description = None
        self.story = None
        self.voice_url = None
        self.tags = None
        self.price = Decimal("10.00")
        self.gender = None
        self.discount_events = events


def _svc() -> CharacterService:
    return CharacterService.__new__(CharacterService)  # 조회 헬퍼만 쓴다(DB 불요)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_summary_schema_exposes_active_discount():
    assert "active_discount" in CharacterSummary.model_fields
    # 상세는 승격된 필드를 상속받는다(중복 선언 없음).
    assert "active_discount" in CharacterDetail.model_fields


def test_active_discount_is_carried_into_summary():
    end = _now() + timedelta(hours=24)
    c = _Char([_Ev(Decimal("5.00"), _now() - timedelta(minutes=1), end)])
    out = _svc()._to_summary(c, owned=False)
    assert out.effective_price == Decimal("5.00")
    assert out.active_discount is not None
    assert out.active_discount.end_time == end, "카운트다운 마감 시각이 목록에 없다"


@pytest.mark.parametrize(
    "ev, why",
    [
        (None, "할인 이벤트 자체가 없음"),
        (_Ev(Decimal("5.00"), datetime.now(timezone.utc) - timedelta(days=2),
             datetime.now(timezone.utc) - timedelta(days=1)), "기간 종료"),
        (_Ev(Decimal("5.00"), datetime.now(timezone.utc) + timedelta(days=1),
             datetime.now(timezone.utc) + timedelta(days=2)), "아직 시작 전"),
        (_Ev(Decimal("5.00"), datetime.now(timezone.utc) - timedelta(hours=1),
             datetime.now(timezone.utc) + timedelta(hours=1), False), "activate=False"),
        (_Ev(None, datetime.now(timezone.utc) - timedelta(hours=1),
             datetime.now(timezone.utc) + timedelta(hours=1)), "할인가 없음"),
    ],
)
def test_inactive_discount_leaves_price_untouched(ev, why):
    """적용 조건을 하나라도 어기면 할인 미노출 + 정가 유지 — 앱은 그때 배지를 안 그린다."""
    out = _svc()._to_summary(_Char([] if ev is None else [ev]), owned=False)
    assert out.active_discount is None, why
    assert out.effective_price == out.price == Decimal("10.00"), why
