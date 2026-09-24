"""R3-b(2026-09-24, bt-back) — 옛 `pro`/`max` 가 DB 에 다시 들어와도 결제자가 조용히
Free 로 떨어지면 안 된다.

배경: DB 는 공유다(app-api 구코드는 아직 `plan='pro'|'max'` 를 쓴다). premium
브랜치(D1 2단화)의 plan 해석표는 `None`/`"premium"` 두 키뿐이라(테이블은 절대
안 늘린다 — `test_plan_two_tier.py::test_engine_tables_have_only_free_and_premium_
keys` 가 잠근다), 그 값이 넘어오면 "모르는 플랜"으로 오인해 Free 로 떨어졌다.
`subscription_status._from_row`(단 하나의 해석 지점, R3-b 커밋 참조)에서
`normalize_plan` 으로 접어 막는다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401 - 전 모델 import
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.subscribe import Subscribe
from domains.commerce.models.voice import Voice
from domains.commerce.service import entitlements
from domains.commerce.service.plan_normalize import normalize_plan
from domains.commerce.service.subscription_status import resolve_status
from domains.learning.service import call_service as cs


# --------------------------------------------------------------------------- #
# 1) normalize_plan / resolve_status — 순수 함수 단위 시험
# --------------------------------------------------------------------------- #
def test_normalize_plan_folds_legacy_tiers_into_premium():
    assert normalize_plan("pro") == "premium"
    assert normalize_plan("max") == "premium"


def test_normalize_plan_leaves_premium_and_unknown_values_alone():
    """⛔ 진짜 모르는 값(DB 오염)까지 premium 으로 접으면 안 된다 — Free 폴백이
    하류(call_service 표)에서 정상 작동해야 하므로 그대로 통과시킨다."""
    assert normalize_plan("premium") == "premium"
    assert normalize_plan("xyz") == "xyz"


@pytest.fixture()
def _row():
    from dataclasses import dataclass
    from decimal import Decimal
    from typing import Optional

    @dataclass
    class Row:
        subscribe_id: int = 1
        start_date: Optional[datetime] = None
        end_date: Optional[datetime] = None
        price: Optional[Decimal] = None
        is_activate: Optional[bool] = True
        plan: str = "pro"
        is_trial: bool = False
        billing_state: str = "ok"
        retrying_until: Optional[datetime] = None
        paused_since: Optional[datetime] = None

    return Row


def test_resolve_status_normalizes_a_legacy_pro_row(_row):
    now = datetime.now(timezone.utc)
    row = _row(plan="pro", end_date=now + timedelta(days=10))
    assert resolve_status([row], now=now).plan == "premium"


def test_resolve_status_normalizes_a_legacy_max_row(_row):
    now = datetime.now(timezone.utc)
    row = _row(plan="max", end_date=now + timedelta(days=10))
    assert resolve_status([row], now=now).plan == "premium"


def test_resolve_status_premium_row_is_unchanged_regression(_row):
    now = datetime.now(timezone.utc)
    row = _row(plan="premium", end_date=now + timedelta(days=10))
    assert resolve_status([row], now=now).plan == "premium"


# --------------------------------------------------------------------------- #
# 2) 실통합 — 실제 DB 행(plan='pro'/'max') → effective_plan → call_service 표
#    (⛔⛔ bt-back 재현 그대로: 영상 False·조각 1·예산 300초로 조용히 강등되던 자리)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def ctx():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    v = Voice(name="V", gender="male"); db.add(v); db.flush()
    ch = Character(name="바바", role="선생님", personality="시크", voice_id=v.voice_id, price=0)
    db.add(ch); db.flush()
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-legacy")
    db.add(m); db.commit()
    return {"db": db, "member_id": m.member_id}


def _active_sub(ctx, plan):
    now = datetime.now(timezone.utc)
    sub = Subscribe(
        member_id=ctx["member_id"], plan=plan, is_activate=True,
        start_date=now, end_date=now + timedelta(days=10),
        billing_state="ok", is_trial=False,
    )
    ctx["db"].add(sub); ctx["db"].commit()
    return sub


@pytest.mark.parametrize("legacy_plan", ["pro", "max"])
def test_legacy_plan_row_resolves_to_full_premium_entitlement(ctx, legacy_plan):
    """⛔⛔ 핵심 재현·수정 확인(bt-back) — plan='pro'/'max' 행이 있는 회원은
    effective_plan == 'premium' · 영상 True · 조각 3 · 예산 900초여야 한다(이전엔
    전부 Free 값으로 조용히 강등됐다)."""
    _active_sub(ctx, legacy_plan)
    db, member_id = ctx["db"], ctx["member_id"]

    assert entitlements.effective_plan(db, member_id) == "premium"
    assert cs.call_video_for(db, member_id) is True
    assert cs.call_fragments_for_member(db, member_id) == 3
    assert cs.daily_budget_s(entitlements.effective_plan(db, member_id)) == 900


def test_premium_plan_row_is_unchanged_regression(ctx):
    """회귀 — plan='premium' 행은 그대로 premium 대우를 받는다(정규화가 무영향)."""
    _active_sub(ctx, "premium")
    db, member_id = ctx["db"], ctx["member_id"]

    assert entitlements.effective_plan(db, member_id) == "premium"
    assert cs.call_video_for(db, member_id) is True
    assert cs.call_fragments_for_member(db, member_id) == 3


def test_truly_unknown_plan_value_still_falls_back_to_free(ctx):
    """⛔ 진짜 모르는 값(DB 오염, 예 'xyz')은 여전히 Free 로 떨어진다(R5) — 정규화가
    Free 폴백 자체를 없애면 안 된다."""
    _active_sub(ctx, "xyz")
    db, member_id = ctx["db"], ctx["member_id"]

    assert cs.call_video_for(db, member_id) is False
    assert cs.call_fragments_for_member(db, member_id) == 1
    assert cs.daily_budget_s(entitlements.effective_plan(db, member_id)) == 300
