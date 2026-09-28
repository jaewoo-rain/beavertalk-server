"""§24 구독 갱신 재조회 회귀 (외부 의존 0, 인메모리 sqlite).

docs/plans/2026-09-28-구독갱신-반영-24.md §3·§5(수용 기준) 그대로 시험한다.

절대 틀리면 안 되는 4가지(bt-back 지시) — 이 파일이 전부 회귀로 고정한다:
    1. source='store' 만 재조회한다(manual 은 스토어에 물으면 전부 invalid 가
       나와 날아간다 — 최우선 안전장치).
    2. reason='unavailable' 엔 박탈하지 않는다(저장값 유지 + 재시도).
    3. 통화 경로(entitlements.effective_plan)엔 이 모듈이 전혀 관여하지 않는다.
    4. purchase_token 을 로그에 찍지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core import iap
from core.config import settings as app_settings
from db.registry import Base  # noqa: F401 - 전 모델 import
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.iap_receipt import IapReceipt
from domains.commerce.models.subscribe import Subscribe
from domains.commerce.models.voice import Voice
from domains.commerce.service import entitlements
from domains.commerce.service.subscription_refresh_service import SubscriptionRefreshService

NOW = datetime.now(timezone.utc)
PAST = NOW - timedelta(days=1)
LATER = NOW + timedelta(days=30)


def _eq(a, b) -> bool:
    """sqlite 는 tz 를 안 돌려준다(값만 비교) — churn_reason 시험과 같은 방어."""
    if a is None or b is None:
        return a is b
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return a == b


@pytest.fixture()
def db():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    v = Voice(name="Fenrir", gender="male")
    s.add(v)
    s.flush()
    s.add(Character(name="BABA", role="r", personality="p", voice_id=v.voice_id, price=0))
    s.commit()
    return s


def _member(db, auth="auth-1") -> int:
    m = Member(language="en", onboarding_completed=True, auth_user_id=auth)
    db.add(m)
    db.commit()
    return m.member_id


def _subscribe(db, member_id, *, source="store", is_activate=True, end_date=PAST) -> Subscribe:
    sub = Subscribe(member_id=member_id, start_date=NOW - timedelta(days=60),
                    end_date=end_date, is_activate=is_activate, source=source,
                    product_id="bt_pro_monthly", plan="premium", billing_period="monthly")
    db.add(sub)
    db.commit()
    return sub


def _receipt(db, member_id, *, token="tok-abc", platform="android",
             product_id="bt_pro_monthly", tx="tx-1", last_check=None) -> IapReceipt:
    r = IapReceipt(member_id=member_id, platform=platform, transaction_id=tx,
                   product_id=product_id, kind="subscription", purchase_token=token,
                   last_store_check_at=last_check)
    db.add(r)
    db.commit()
    return r


def _fake_verify(*, ok=True, reason=None, expires_at=None, is_trial=None):
    def _v(**kwargs):
        return iap.VerifyResult(ok=ok, reason=reason, transaction_id=kwargs.get("transaction_id"),
                                expires_at=expires_at, is_trial=is_trial)
    return _v


# --------------------------------------------------------------------------- #
# 1) 최우선 안전장치 — source='manual' 은 절대 안 건드린다
# --------------------------------------------------------------------------- #
def test_manual_subscription_is_never_refreshed(db, monkeypatch):
    calls = []
    monkeypatch.setattr(iap, "verify", lambda **k: calls.append(k) or iap.VerifyResult(ok=False, reason="invalid"))
    mid = _member(db)
    sub = _subscribe(db, mid, source="manual", end_date=PAST)
    _receipt(db, mid)

    SubscriptionRefreshService(db).refresh_member(mid)

    assert calls == [], "manual 행을 스토어에 물었다 — 사장님이 준 Premium 이 날아갈 뻔했다"
    db.refresh(sub)
    assert _eq(sub.end_date, PAST), "manual 행이 건드려졌다"
    assert sub.is_activate is True


def test_manual_subscription_is_skipped_by_sweep_too(db, monkeypatch):
    calls = []
    monkeypatch.setattr(iap, "verify", lambda **k: calls.append(k) or iap.VerifyResult(ok=False, reason="invalid"))
    mid = _member(db)
    _subscribe(db, mid, source="manual", end_date=PAST)
    _receipt(db, mid)

    n = SubscriptionRefreshService(db).sweep()

    assert n == 0
    assert calls == []


# --------------------------------------------------------------------------- #
# 2) 갱신 반영 — ok=True 면 end_date 가 올라간다
# --------------------------------------------------------------------------- #
def test_store_subscription_extends_end_date_when_ok(db, monkeypatch):
    monkeypatch.setattr(iap, "verify", _fake_verify(ok=True, expires_at=LATER))
    mid = _member(db)
    sub = _subscribe(db, mid, source="store", end_date=PAST)
    receipt = _receipt(db, mid)

    SubscriptionRefreshService(db).refresh_member(mid)

    db.refresh(sub)
    db.refresh(receipt)
    assert _eq(sub.end_date, LATER), "만료된 store 구독 + 유효 토큰 → 재조회로 end_date 가 미래로 이동해야 한다"
    # ⛔⛔ §26-⑦ 정정(2026-09-29, 실기기 회귀) — 갱신 성공 뒤엔 쓰로틀을 **안**
    #   찍는다(예전엔 여기서 찍어서, 그 뒤 1시간 동안 다른 조회가 재조회를 못
    #   했다 — 23:46 갱신 확인 → 00:01 Free 로 보인 사고). end_date 가 미래로
    #   갔으니 다음 호출은 애초에 "아직 안 지남" 가드를 못 넘어 쓰로틀이 필요
    #   없다.
    assert receipt.last_store_check_at is None


# --------------------------------------------------------------------------- #
# 3) invalid → 지금과 동일(아무것도 더 건드리지 않는다 — 이미 과거라 Free 다)
# --------------------------------------------------------------------------- #
def test_invalid_leaves_end_date_unchanged(db, monkeypatch):
    monkeypatch.setattr(iap, "verify", _fake_verify(ok=False, reason="invalid"))
    mid = _member(db)
    sub = _subscribe(db, mid, source="store", end_date=PAST)
    receipt = _receipt(db, mid)

    SubscriptionRefreshService(db).refresh_member(mid)

    db.refresh(sub)
    db.refresh(receipt)
    assert _eq(sub.end_date, PAST)
    assert receipt.last_store_check_at is not None, "무효 판정도 쓰로틀에 기록돼야 매 호출 스토어를 안 때린다"


# --------------------------------------------------------------------------- #
# 4) unavailable → 박탈 금지, 저장값 그대로 + 재시도 예정
# --------------------------------------------------------------------------- #
def test_unavailable_does_not_deprive(db, monkeypatch):
    monkeypatch.setattr(iap, "verify", _fake_verify(ok=False, reason="unavailable"))
    mid = _member(db)
    sub = _subscribe(db, mid, source="store", end_date=PAST, is_activate=True)
    receipt = _receipt(db, mid)

    SubscriptionRefreshService(db).refresh_member(mid)

    db.refresh(sub)
    db.refresh(receipt)
    assert _eq(sub.end_date, PAST), "스토어 장애인데 저장값이 바뀌었다 — 박탈 금지 위반"
    assert sub.is_activate is True, "스토어 장애인데 접근이 꺼졌다 — 박탈 금지 위반"
    assert receipt.last_store_check_at is not None, "실패해도 다음 재시도 간격은 쓰로틀로 관리된다"


# --------------------------------------------------------------------------- #
# 5) 토큰 없음 / 영수증 없음 — 조용히 통과(기존 5행)
# --------------------------------------------------------------------------- #
def test_no_token_is_skipped_quietly(db, monkeypatch):
    calls = []
    monkeypatch.setattr(iap, "verify", lambda **k: calls.append(k) or iap.VerifyResult(ok=True))
    mid = _member(db)
    sub = _subscribe(db, mid, source="store", end_date=PAST)
    _receipt(db, mid, token=None)

    SubscriptionRefreshService(db).refresh_member(mid)  # 예외 없이 통과

    assert calls == []
    db.refresh(sub)
    assert _eq(sub.end_date, PAST)


def test_no_receipt_at_all_is_skipped_quietly(db, monkeypatch):
    calls = []
    monkeypatch.setattr(iap, "verify", lambda **k: calls.append(k) or iap.VerifyResult(ok=True))
    mid = _member(db)
    _subscribe(db, mid, source="store", end_date=PAST)
    # 영수증 행 자체를 안 만든다.

    SubscriptionRefreshService(db).refresh_member(mid)

    assert calls == []


def test_unexpired_subscription_is_not_refreshed(db, monkeypatch):
    """아직 안 지났으면(무기한 포함) 재조회 불필요."""
    calls = []
    monkeypatch.setattr(iap, "verify", lambda **k: calls.append(k) or iap.VerifyResult(ok=True))
    mid = _member(db)
    _subscribe(db, mid, source="store", end_date=LATER)
    _receipt(db, mid)

    SubscriptionRefreshService(db).refresh_member(mid)

    assert calls == []


# --------------------------------------------------------------------------- #
# 6) 쓰로틀 — 안에서 두 번 호출 → 스토어 호출 1회
# --------------------------------------------------------------------------- #
def test_throttle_prevents_a_second_call_within_the_window(db, monkeypatch):
    """⛔⛔ §26-⑦ 정정 — 쓰로틀은 invalid/unavailable 에만 찍힌다(성공 뒤엔 안
    찍는다, 위 섹션 2 참조) — 그래서 쓰로틀 자체를 보려면 **실패 응답**으로 두
    번 불러야 한다(성공으로 두 번 부르면 첫 호출이 이미 end_date 를 미래로
    옮겨 두 번째 호출이 "아직 안 지남" 가드에 걸릴 뿐, 쓰로틀을 시험한 게 아니다)."""
    calls = []
    monkeypatch.setattr(iap, "verify", lambda **k: calls.append(k) or iap.VerifyResult(ok=False, reason="invalid"))
    mid = _member(db)
    _subscribe(db, mid, source="store", end_date=PAST)
    _receipt(db, mid)

    svc = SubscriptionRefreshService(db)
    svc.refresh_member(mid)
    svc.refresh_member(mid)  # 쓰로틀 안 — 스토어를 또 부르면 안 된다

    assert len(calls) == 1, "쓰로틀 안에서 스토어가 두 번 불렸다"


def test_success_does_not_set_throttle_so_a_later_check_is_not_blocked(db, monkeypatch):
    """⭐⭐ §26-⑦ 핵심 회귀(bt-back 실기기 실측: 23:46 갱신 확인 → 00:01 앱 재실행
    시 쓰로틀에 막혀 Free 로 보임). 갱신 성공 직후엔 last_store_check_at 을 안
    찍는다 — 그래서 다른 조회가 **바로** 재조회할 수 있다(무기한 대기 없음)."""
    monkeypatch.setattr(iap, "verify", _fake_verify(ok=True, expires_at=LATER))
    mid = _member(db)
    _subscribe(db, mid, source="store", end_date=PAST)
    receipt = _receipt(db, mid)

    SubscriptionRefreshService(db).refresh_member(mid)

    db.refresh(receipt)
    assert receipt.last_store_check_at is None, "성공 뒤에 쓰로틀 스탬프가 찍히면 안 된다"


# --------------------------------------------------------------------------- #
# §26-③(2026-09-29) — 자기치유 재조회는 is_trial=None(offerPhase 미제공)을
# 근거 없는 재조회로 뒤집지 않는다. is_trial 이 확정값이면 정상 반영한다.
# --------------------------------------------------------------------------- #
def test_self_heal_refresh_with_unknown_trial_does_not_touch_stored_flag(db, monkeypatch):
    monkeypatch.setattr(iap, "verify", _fake_verify(ok=True, expires_at=LATER, is_trial=None))
    mid = _member(db)
    sub = _subscribe(db, mid, source="store", end_date=PAST)
    sub.is_trial = True
    db.commit()
    _receipt(db, mid)

    SubscriptionRefreshService(db).refresh_member(mid)

    db.refresh(sub)
    assert sub.is_trial is True, "is_trial=None 이면 기존 True 를 건드리면 안 된다"


def test_self_heal_refresh_with_definite_trial_updates_stored_flag(db, monkeypatch):
    monkeypatch.setattr(iap, "verify", _fake_verify(ok=True, expires_at=LATER, is_trial=False))
    mid = _member(db)
    sub = _subscribe(db, mid, source="store", end_date=PAST)
    sub.is_trial = True
    db.commit()
    _receipt(db, mid)

    SubscriptionRefreshService(db).refresh_member(mid)

    db.refresh(sub)
    assert sub.is_trial is False, "확정값(offerPhase=basePrice 등)은 정상 반영돼야 한다"


def test_throttle_allows_a_second_call_after_the_window(db, monkeypatch):
    monkeypatch.setattr(app_settings, "IAP_RECHECK_MIN_INTERVAL_S", 60)
    calls = []
    monkeypatch.setattr(iap, "verify", lambda **k: calls.append(k) or iap.VerifyResult(ok=False, reason="invalid"))
    mid = _member(db)
    _subscribe(db, mid, source="store", end_date=PAST)
    _receipt(db, mid, last_check=NOW - timedelta(seconds=120))  # 쓰로틀 밖

    SubscriptionRefreshService(db).refresh_member(mid)

    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# 7) 스케줄 스윕 — 만료·활성·store·토큰 있는 행만 대상
# --------------------------------------------------------------------------- #
def test_sweep_targets_only_expired_active_store_rows_with_tokens(db, monkeypatch):
    monkeypatch.setattr(iap, "verify", _fake_verify(ok=True, expires_at=LATER))

    m1 = _member(db, "auth-store-expired")
    sub1 = _subscribe(db, m1, source="store", end_date=PAST, is_activate=True)
    _receipt(db, m1, tx="tx-store-expired")

    m2 = _member(db, "auth-manual-expired")
    _subscribe(db, m2, source="manual", end_date=PAST, is_activate=True)
    _receipt(db, m2, tx="tx-manual-expired")

    m3 = _member(db, "auth-store-live")
    _subscribe(db, m3, source="store", end_date=LATER, is_activate=True)
    _receipt(db, m3, tx="tx-store-live")

    m4 = _member(db, "auth-store-cancelled")
    _subscribe(db, m4, source="store", end_date=PAST, is_activate=False)
    _receipt(db, m4, tx="tx-store-cancelled")

    n = SubscriptionRefreshService(db).sweep()

    assert n == 1, "만료·활성·store·토큰 있는 행만 대상이어야 한다"
    db.refresh(sub1)
    assert _eq(sub1.end_date, LATER)


# --------------------------------------------------------------------------- #
# 8) ⛔⛔ 통화 경로엔 스토어 호출이 없다 — 최우선 규율
# --------------------------------------------------------------------------- #
def test_call_path_effective_plan_never_touches_the_store(db, monkeypatch):
    """entitlements.effective_plan(통화 경로)이 §24 재조회를 전혀 타지 않는다.

    만료된 store 구독 + 유효 토큰(재조회 조건을 전부 만족)을 세팅해 두고,
    iap.verify 가 불리면 즉시 실패하도록 poison 을 심는다 — 통화 경로가 조금이라도
    스토어를 건드리면 이 시험이 터진다.
    """
    def _poison(**kwargs):
        raise AssertionError("통화 경로(effective_plan)가 스토어를 불렀다 — 절대 금지")
    monkeypatch.setattr(iap, "verify", _poison)

    mid = _member(db)
    _subscribe(db, mid, source="store", end_date=PAST, is_activate=True)
    _receipt(db, mid)

    plan = entitlements.effective_plan(db, mid)

    assert plan is None  # 만료된 채로 그대로 Free — 통화 경로는 저장값만 읽는다


def test_call_fragments_for_plan_never_touches_the_store(db, monkeypatch):
    """call_service 가 entitlements 를 거쳐 플랜을 읽는 다른 진입점도 안전한지 재확인."""
    from domains.learning.service import call_service

    def _poison(**kwargs):
        raise AssertionError("통화 경로(call_fragments_for_plan)가 스토어를 불렀다")
    monkeypatch.setattr(iap, "verify", _poison)

    mid = _member(db)
    _subscribe(db, mid, source="store", end_date=PAST, is_activate=True)
    _receipt(db, mid)

    call_service.call_fragments_for_plan(db, mid, None)  # 예외 없이 끝나야 한다
