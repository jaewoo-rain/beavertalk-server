"""GET /api/v1/calls/{id}/resume-status — can_resume 는 expression·freetalk·chat 에
열리고 level_test 는 닫힌다(2026-09-12, 실기기 1447 / QA C3 재검-① 2026-09-22 / C7
2026-09-23 — chat 도 화이트리스트에 들어왔다, 프리미엄 5분 조각 재연결).

옛 조건이 normal 만 허용해 표현학습·프리토킹은 5분에 «Keep talking» 시트 없이 결과 화면으로
떨어졌다. C3(D3)로 normal→chat 개명, C7 로 chat 도 이어하기 대상이 됐다. 조각 상한·요약
준비 판정은 스텁.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.deps as deps
import domains.learning.routers.call as call_router
from core.config import settings as app_settings
from core.supabase_auth import AuthUser
from db.registry import Base
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call


def _fake_verify(token):
    return AuthUser(uid=token, email=f"{token}@test.io") if token and token.startswith("auth-") else None


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    monkeypatch.setattr(deps, "verify_token", _fake_verify)
    monkeypatch.setattr(call_router.call_service, "call_fragments_for_member", lambda db, member_id: 3)   # Pro·Max 상한
    monkeypatch.setattr(call_router.svc, "resume_context_is_fresh", lambda db, call_id: False)


@pytest.fixture()
def env():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            if len(t.primary_key.columns) == 1:
                pk.type = Integer()
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = sf()
    v = Voice(name="Fenrir", gender="male"); db.add(v); db.flush()
    ch = Character(name="비비", role="선생님", personality="다정", voice_id=v.voice_id, price=0); db.add(ch); db.flush()
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-m"); db.add(m); db.commit()
    calls = {}
    for ct, frags in (("chat", 1), ("expression", 1), ("freetalk", 2), ("level_test", 1), ("expression", 3)):
        c = Call(member_id=m.member_id, character_id=ch.character_id, call_type=ct, status="done", fragment_count=frags)
        db.add(c); db.flush()
        calls[(ct, frags)] = c.call_id
    db.commit(); db.close()
    from main import create_app
    app = create_app()
    app.state.session_factory = sf
    app.state.settings = app_settings
    app.state.genai_client = object()
    return TestClient(app), calls


def _status(client, call_id):
    r = client.get(f"/api/v1/calls/{call_id}/resume-status", headers={"Authorization": "Bearer auth-m"})
    assert r.status_code == 200, r.text
    return r.json()


def test_expression_freetalk_and_chat_can_resume(env):
    """⭐⭐ C7(2026-09-23): chat 도 expression·freetalk 와 같은 화이트리스트에 있다."""
    client, calls = env
    for key in (("expression", 1), ("freetalk", 2), ("chat", 1)):
        body = _status(client, calls[key])
        assert body["can_resume"] is True, key
        assert body["fragment_count"] == key[1] and body["max_fragments"] == 3


def test_level_test_never_resumes_and_exhausted_fragments_close_the_door(env):
    client, calls = env
    assert _status(client, calls[("level_test", 1)])["can_resume"] is False
    assert _status(client, calls[("expression", 3)])["can_resume"] is False, "조각 상한 소진"


# ── 플랜 흉내(2026-09-13 사장님: "free 일 때는 연장하면 안 되고 premium 일 때는 연장되도록") ─────────────────────────────
def _status_q(client, call_id, q):
    r = client.get(f"/api/v1/calls/{call_id}/resume-status{q}", headers={"Authorization": "Bearer auth-m"})
    assert r.status_code == 200, r.text
    return r.json()


def test_plan_override_free_closes_resume_for_admin(env, monkeypatch):
    """⚠ QA C3 재검-①: 대상 콜을 expression 으로 쓴다 — chat(옛 normal)은 이제 조각 상한과
    무관하게 can_resume 이 항상 False 라 plan_override 의 조각 상한 효과를 못 가린다."""
    client, calls = env
    monkeypatch.setattr(call_router.call_service, "is_unlimited_member", lambda db, member_id: True)   # admin
    body = _status_q(client, calls[("expression", 1)], "?plan_override=free")
    assert body["can_resume"] is False and body["max_fragments"] == 1, "«Free 로 통화» 는 «Keep talking» 이 없어야 한다"
    body = _status_q(client, calls[("expression", 1)], "?plan_override=premium")
    assert body["can_resume"] is True and body["max_fragments"] == 3


def test_plan_override_ignored_for_non_admin(env, monkeypatch):
    client, calls = env
    monkeypatch.setattr(call_router.call_service, "is_unlimited_member", lambda db, member_id: False)
    body = _status_q(client, calls[("expression", 1)], "?plan_override=free")
    assert body["can_resume"] is True and body["max_fragments"] == 3, "admin 이 아니면 본인 플랜(스텁 3) 그대로"


@pytest.mark.parametrize("stale_plan", ["pro", "max"])
def test_plan_override_rejects_the_old_three_tier_values(env, stale_plan):
    """⛔ D1(2026-09-22) 2단화 — pro·max 는 더 이상 유효한 값이 아니다. 422 로 거절한다."""
    client, calls = env
    r = client.get(
        f"/api/v1/calls/{calls[('expression', 1)]}/resume-status?plan_override={stale_plan}",
        headers={"Authorization": "Bearer auth-m"},
    )
    assert r.status_code == 422


def test_plan_override_rejects_unknown_value(env):
    client, calls = env
    r = client.get(f"/api/v1/calls/{calls[('expression', 1)]}/resume-status?plan_override=vip", headers={"Authorization": "Bearer auth-m"})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# C5(2026-09-23) — 조각 상한이 남아도 하루 예산이 없으면 이어할 수 없다
# --------------------------------------------------------------------------- #
def test_can_resume_is_false_when_the_daily_budget_is_exhausted(env, monkeypatch):
    """⭐⭐ 조각 상한(3)이 넉넉히 남아도(fragment_count=1) remaining_budget_s=0 이면
    can_resume 이 False 다 — WS 재개 자체가 DAILY_LIMIT 로 거절될 것이기 때문이다."""
    client, calls = env
    monkeypatch.setattr(
        call_router.call_service, "remaining_budget_s",
        lambda db, member_id, tz=None, tz_offset_min=None, plan_override=None, resuming_call_id=None: 0,
    )
    body = _status(client, calls[("expression", 1)])
    assert body["can_resume"] is False, "예산 소진인데 이어하기가 열렸다"


def test_can_resume_ignores_the_budget_check_when_exempt(env, monkeypatch):
    """remaining_budget_s 가 None(admin 면제)이면 예산 조건 자체를 걸지 않는다."""
    client, calls = env
    monkeypatch.setattr(
        call_router.call_service, "remaining_budget_s",
        lambda db, member_id, tz=None, tz_offset_min=None, plan_override=None, resuming_call_id=None: None,
    )
    body = _status(client, calls[("expression", 1)])
    assert body["can_resume"] is True


# --------------------------------------------------------------------------- #
# R2-b(2026-09-24, 프론트 실기기 QA) — resume-status 만 tz 를 안 받아 daily-status·WS
# 와 최대 9시간(KST) 어긋났다("이 화면은 된다는데 서버는 거절" 류). tz·tz_offset_min·
# resuming_call_id 를 실제로 remaining_budget_s 에 넘기는지 배선을 확인한다.
# --------------------------------------------------------------------------- #
def test_resume_status_passes_tz_and_resuming_call_id_through_to_remaining_budget_s(env, monkeypatch):
    """⛔⛔ 배선 확인 — 이 엔드포인트가 받은 tz·tz_offset_min 그대로, 그리고 자기
    자신의 call_id 를 resuming_call_id 로(R2-a, 자정 걸친 체인의 남은 예산을 이
    화면에서도 정확히 보여준다) remaining_budget_s 에 넘겨야 한다."""
    client, calls = env
    seen = {}

    def spy(db, member_id, tz=None, tz_offset_min=None, plan_override=None, resuming_call_id=None):
        seen.update(tz=tz, tz_offset_min=tz_offset_min, resuming_call_id=resuming_call_id)
        return 100

    monkeypatch.setattr(call_router.call_service, "remaining_budget_s", spy)
    call_id = calls[("expression", 1)]
    body = _status_q(client, call_id, "?tz=Asia/Seoul&tz_offset_min=540")
    assert seen == {"tz": "Asia/Seoul", "tz_offset_min": 540, "resuming_call_id": call_id}
    assert body["can_resume"] is True   # remaining=100>0, 배선만 확인


def test_resume_status_tz_is_optional_and_defaults_harmlessly(env, monkeypatch):
    """tz·tz_offset_min 을 안 보내면(옛 클라·구버전 앱) None 그대로 넘어가야 한다
    (local_window_utc 가 그 경우 UTC 로 안전하게 폴백하는 건 별도 시험 — 여기선
    이 엔드포인트가 값을 조작하지 않고 그대로 통과시키는지만 본다)."""
    client, calls = env
    seen = {}

    def spy(db, member_id, tz=None, tz_offset_min=None, plan_override=None, resuming_call_id=None):
        seen.update(tz=tz, tz_offset_min=tz_offset_min)
        return 100

    monkeypatch.setattr(call_router.call_service, "remaining_budget_s", spy)
    _status(client, calls[("expression", 1)])
    assert seen == {"tz": None, "tz_offset_min": None}


def test_daily_status_and_resume_status_agree_on_exhausted_budget_with_the_same_tz(env, monkeypatch):
    """⛔⛔ R2-b 핵심 — 예전엔 resume-status 만 UTC 자정 기준이라, KST(UTC+9)에서
    daily-status(tz 를 이미 받는다)와 최대 9시간 어긋날 수 있었다. 같은 tz 를 주면
    두 엔드포인트가 **같은 예산 소진 판정**을 내야 한다.

    시나리오: 이 통화의 call_date 를 "오늘"(KST) 자정 막 지난 시각으로 두고
    total_time 을 premium 한도(900) 전부로 채운다 — KST 로는 명백히 "오늘 다 썼다"
    인데, tz 를 안 받던 옛 resume-status(UTC 자정 기준)는 이 시각을 "어제"로 볼 수
    있어(KST 자정 0~9시는 UTC 로 전날) 예산이 안 깎인 것처럼 잘못 봤다.
    """
    client, calls = env
    monkeypatch.setattr(call_router.call_service, "is_unlimited_member", lambda db, member_id: False)
    monkeypatch.setattr(
        "domains.commerce.service.entitlements.effective_plan",
        lambda db, member_id: "premium",
    )

    zone = ZoneInfo("Asia/Seoul")
    today_kst = datetime.now(zone).date()
    call_date_utc = datetime.combine(today_kst, time.min, tzinfo=zone).astimezone(timezone.utc) \
        + timedelta(minutes=30)   # KST 00:30 — UTC 로는 전날 오후(9시간 차)

    call_id = calls[("expression", 1)]
    sf = client.app.state.session_factory
    db = sf()
    row = db.get(Call, call_id)
    row.call_date = call_date_utc
    row.total_time = 900   # premium 한도 전부 소진
    db.commit()
    db.close()

    resume_body = _status_q(client, call_id, "?tz=Asia/Seoul&tz_offset_min=540")
    daily_body = client.get(
        f"/api/v1/calls/daily-status?date={today_kst.isoformat()}&tz=Asia/Seoul&tz_offset=540",
        headers={"Authorization": "Bearer auth-m"},
    ).json()

    assert resume_body["can_resume"] is False, "KST 로 오늘 예산을 다 썼는데 resume-status 가 열어줬다"
    assert daily_body["can_call_normal"] is False, "daily-status 도 같은 판정이어야 한다(비교 기준)"
