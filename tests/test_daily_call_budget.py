"""C4(2026-09-23, D4) — 일일 통화 예산(분) 서버 판정.

Free 300s(5분) · Premium 900s(15분), chat·expression·freetalk 합산(레벨테스트 제외).
근거: docs/plans/2026-09-22-프리미엄-자유대화-15분-달력.md(C4)

시험 목록(문서 그대로):
    - Free 300 소진 → 거절
    - premium 에서 Free 로 쓴 300 이 차감돼 600 남음("결제 직후 10분")
    - 레벨테스트는 차감 안 함
    - 조각2 시작 시 남은 0 이면 거절(WS 레벨은 test_normalcall_ws.py 쪽에서 잡는다)
    - 서머타임 경계(America/New_York) 자정 계산
    - admin 면제 / admin+override 적용
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.repository.call_repository import CallRepository
from domains.learning.service import call_service as cs
from domains.learning.service import normalcall_service as ns


# --------------------------------------------------------------------------- #
# 1) daily_budget_s — 플랜별 예산(초)
# --------------------------------------------------------------------------- #
def test_budget_is_300_free_900_premium():
    assert cs.daily_budget_s(None) == 300
    assert cs.daily_budget_s("premium") == 900


def test_budget_falls_back_to_free_for_unknown_plan():
    """R5 — 모르는 플랜은 Free 로 떨어진다(무제한이 새는 것보다 싸다)."""
    assert cs.daily_budget_s("vip") == 300


# --------------------------------------------------------------------------- #
# 2) local_window_utc — IANA 존 우선, 서머타임 경계 포함
# --------------------------------------------------------------------------- #
def test_local_window_utc_prefers_iana_over_offset():
    """같은 지역이라도 tz(IANA)가 있으면 tz_offset_min 은 무시된다."""
    s, e = cs.local_window_utc(date(2026, 7, 29), "Asia/Seoul", 0)
    assert s == datetime(2026, 7, 28, 15, tzinfo=timezone.utc)
    assert e == datetime(2026, 7, 29, 15, tzinfo=timezone.utc)


def test_local_window_utc_falls_back_to_offset_on_bad_tz_name(caplog):
    """잘못된 존 이름 하나로 통화 시작이 막히면 안 된다(R5) — 경고 로그 + 폴백."""
    with caplog.at_level("WARNING"):
        s, e = cs.local_window_utc(date(2026, 7, 29), "Not/A_Zone", 540)
    assert s == datetime(2026, 7, 28, 15, tzinfo=timezone.utc)
    assert "잘못된 tz" in caplog.text


def test_local_window_utc_falls_back_to_utc_when_neither_given():
    s, e = cs.local_window_utc(date(2026, 7, 29), None, None)
    assert s == datetime(2026, 7, 29, tzinfo=timezone.utc)
    assert e == datetime(2026, 7, 30, tzinfo=timezone.utc)


def test_local_window_utc_none_date_means_today_in_that_zone():
    """local_date 없으면 그 존 기준 '지금'의 날짜 — 예산 잔여 판정은 항상 지금을 본다."""
    zone = ZoneInfo("Asia/Seoul")
    today = datetime.now(zone).date()
    s, _e = cs.local_window_utc(None, "Asia/Seoul", None)
    expected_start = datetime.combine(today, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
    assert s == expected_start


def test_local_window_utc_handles_dst_spring_forward_boundary():
    """⭐⭐ 서머타임 경계 — America/New_York 2026-03-08(둘째 일요일) 자정은 EST(-05:00),
    다음날 자정은 이미 EDT(-04:00)다. 고정 오프셋(daily_window_utc)이면 하루가
    23시간이 되는 그 경계에서 어긋난다 — local_window_utc 는 ZoneInfo 로 각 자정을
    astimezone 시점에 다시 계산해 정확하다.
    """
    zone = ZoneInfo("America/New_York")
    s, e = cs.local_window_utc(date(2026, 3, 8), "America/New_York", None)
    assert s == datetime(2026, 3, 8, 0, tzinfo=zone).astimezone(timezone.utc)
    assert e == datetime(2026, 3, 9, 0, tzinfo=zone).astimezone(timezone.utc)
    # 이 특정 하루는 서머타임으로 23시간이다 — 위 값을 박아두지 않고 zoneinfo 자체로 검증한다.
    assert (e - s) == timedelta(hours=23)


# --------------------------------------------------------------------------- #
# 3) used_seconds_today / daily_budget_exceeded — DB 통합(인메모리 sqlite)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def session_factory():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def ctx(session_factory):
    db = session_factory()
    voice = Voice(name="V", gender="male"); db.add(voice); db.flush()
    ch = Character(name="바바", role="선생님", personality="시크", voice_id=voice.voice_id, price=0)
    db.add(ch); db.flush()
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-budget")
    db.add(m); db.flush()
    return {"db": db, "member_id": m.member_id, "cid": ch.character_id}


def _call(ctx, *, total_time, call_type="chat", status="done", when_utc=None):
    when_utc = when_utc or datetime.now(timezone.utc)
    c = Call(member_id=ctx["member_id"], character_id=ctx["cid"], call_date=when_utc,
              total_time=total_time, status=status, call_type=call_type)
    ctx["db"].add(c); ctx["db"].commit()
    return c


@pytest.fixture(autouse=True)
def _budget_enforced(monkeypatch):
    """이 파일은 예산 판정 자체를 시험하므로 스위치를 명시로 켠다(기본 True 지만
    다른 파일이 monkeypatch 로 끄고 되돌리는 경합을 피하려 여기서도 못박는다)."""
    monkeypatch.setattr(cs.settings, "DAILY_BUDGET_ENFORCED", True, raising=False)


def test_free_budget_exhausted_after_300s(ctx):
    """⭐ Free 300 소진 → 거절."""
    assert cs.used_seconds_today(ctx["db"], ctx["member_id"]) == 0
    assert cs.daily_budget_exceeded(ctx["db"], ctx["member_id"]) is False

    _call(ctx, total_time=300)
    assert cs.used_seconds_today(ctx["db"], ctx["member_id"]) == 300
    assert cs.daily_budget_exceeded(ctx["db"], ctx["member_id"]) is True


def test_upgrading_to_premium_mid_day_leaves_600s_remaining(ctx, monkeypatch):
    """⭐⭐ "결제 직후 10분" — Free 로 5분(300s)을 이미 쓰고 그날 premium 을 사면,
    같은 날 쓴 300s 가 새 예산(900s)에서 그대로 빠져 남은 게 600s 다. 별도
    "결제 시각 이후" 로직 없이 used_seconds_today 가 하루 전체를 보기 때문에 성립한다.
    """
    _call(ctx, total_time=300)   # Free 로 이미 다 씀
    assert cs.daily_budget_exceeded(ctx["db"], ctx["member_id"]) is True   # 아직 Free

    monkeypatch.setattr(
        "domains.commerce.service.entitlements.effective_plan",
        lambda db, member_id: "premium",
    )
    assert cs.daily_budget_exceeded(ctx["db"], ctx["member_id"]) is False   # premium 900 - 300 = 600 남음

    remaining = cs.daily_budget_s("premium") - cs.used_seconds_today(ctx["db"], ctx["member_id"])
    assert remaining == 600


def test_level_test_does_not_consume_the_budget(ctx):
    """★ 레벨테스트는 예산 축과 무관한 측정 통화라 차감하지 않는다."""
    _call(ctx, total_time=300, call_type="level_test")
    assert cs.used_seconds_today(ctx["db"], ctx["member_id"]) == 0
    assert cs.daily_budget_exceeded(ctx["db"], ctx["member_id"]) is False


def test_ongoing_call_with_recorded_total_time_counts_as_in_progress_spend(ctx):
    """아직 저장이 끝나지 않은 ongoing 도 진행 중인 소비다 — 빼면 끊고 바로 또 거는 구멍."""
    _call(ctx, total_time=300, status="ongoing")
    assert cs.used_seconds_today(ctx["db"], ctx["member_id"]) == 300


def test_ongoing_call_with_null_total_time_is_estimated_from_elapsed_time(ctx):
    """⭐⭐ QA C4 재검-②(2026-09-23): `total_time` 이 아직 NULL(정말 진행 중이라 저장이
    안 끝난 ongoing)이면 SUM 에서 **0으로 세어져** 예산 검사를 통과해 버린다(동시 접속
    경합 시 우회 구멍) — call_date 로부터 지금까지 경과한 시간으로 추정해 채운다.
    """
    started = datetime.now(timezone.utc) - timedelta(seconds=120)
    _call(ctx, total_time=None, status="ongoing", when_utc=started)
    used = cs.used_seconds_today(ctx["db"], ctx["member_id"])
    assert 118 <= used <= 130, "NULL total_time 이 경과 추정 없이 0으로 세어졌다(그 회귀)"


def test_ongoing_call_finishing_does_not_double_count(ctx):
    """⭐ ongoing 이던 통화가 끝나 `total_time` 이 실제 값으로 박히면, 그 이후 집계는
    실제 값만 세고 **경과 추정과 이중으로 더해지지 않는다**(NULL 일 때만 추정한다)."""
    started = datetime.now(timezone.utc) - timedelta(seconds=400)  # 추정치라면 300 초과일 시각
    call = _call(ctx, total_time=None, status="ongoing", when_utc=started)
    call.total_time = 300     # 통화 종료 저장 — 실제 값 확정
    call.status = "done"
    ctx["db"].commit()
    assert cs.used_seconds_today(ctx["db"], ctx["member_id"]) == 300


def test_done_call_with_null_total_time_is_not_estimated(ctx):
    """⭐⭐ QA C4 재검-②(2026-09-23, 재재검): 경과 추정은 **status=='ongoing' 에만**
    건다 — done·analyzing 인데 `total_time` 이 NULL(예: 분석 실패로 못 채움)인 행까지
    추정하면, 이미 끝난 통화인데 **쿼리할 때마다 elapsed 가 계속 자라** 예산을 점점
    더 깎는 별개의 버그가 된다(옛 코드가 status 를 안 보고 NULL 여부만 봤다).
    """
    long_ago = datetime.now(timezone.utc) - timedelta(hours=5)
    _call(ctx, total_time=None, status="done", when_utc=long_ago)
    assert cs.used_seconds_today(ctx["db"], ctx["member_id"]) == 0, \
        "끝난 통화(done)의 NULL total_time 이 경과 시간으로 잘못 추정됐다"


def test_fragment_2_is_rejected_when_remaining_is_zero(ctx):
    """⭐⭐ 조각2 시작 시 남은 예산이 0이면 거절 — 예산 방식은 이어하기 조각도 검사한다
    (call_session.py 의 실제 WS 라우팅 레벨 회귀는 test_normalcall_ws.py
    test_resume_fragment_is_checked_against_the_budget 가 잡는다). 여기서는 그 판정이
    근거로 삼는 함수 자체가 조각 누적 total_time 을 정확히 반영하는지 확인한다.
    ⚠ total_time 은 12차부터 조각 누적이다 — 한 행에 조각1+조각2 길이가 쌓인다.
    """
    _call(ctx, total_time=300, call_type="expression")   # 조각1 로 이미 예산 소진
    assert cs.daily_budget_exceeded(ctx["db"], ctx["member_id"]) is True


def test_admin_is_exempt_from_the_budget(ctx):
    """⭐ admin 은 예산도 면제한다(횟수 면제와 같은 축, is_unlimited_member)."""
    _call(ctx, total_time=300)
    m = ctx["db"].get(Member, ctx["member_id"])
    m.role = "admin"
    ctx["db"].commit()
    assert cs.daily_budget_exceeded(ctx["db"], ctx["member_id"]) is False


def test_admin_with_plan_override_gets_that_plans_budget_enforced(ctx):
    """⭐⭐ admin 이 `plan_override` 를 보내면 **면제가 풀리고** 그 플랜 예산이 적용된다
    (개발자 도구로 한도를 시험할 수 있게) — plan_override 는 plan_override_for 를 거친
    값이어야 한다(여기서는 이미 검증됐다고 가정하고 직접 넘긴다).
    """
    _call(ctx, total_time=300)
    m = ctx["db"].get(Member, ctx["member_id"])
    m.role = "admin"
    ctx["db"].commit()

    # override 없으면 면제(위 시험과 동일 전제)
    assert cs.daily_budget_exceeded(ctx["db"], ctx["member_id"]) is False
    # override=free 면 admin 도 Free 예산(300) 그대로 걸린다
    assert cs.daily_budget_exceeded(ctx["db"], ctx["member_id"], plan_override="free") is True
    # override=premium 이면 900 - 300 = 600 남아 안 걸린다
    assert cs.daily_budget_exceeded(ctx["db"], ctx["member_id"], plan_override="premium") is False


def test_switch_gate_matches_is_daily_limit_reached_discipline(ctx, monkeypatch):
    """DAILY_BUDGET_ENFORCED 가 꺼지고 ENV != prod 면 예산을 다 써도 안 막는다.
    prod 는 스위치와 무관하게 계속 막는다(is_daily_limit_reached 와 같은 이중 게이트)."""
    _call(ctx, total_time=300)
    monkeypatch.setattr(cs.settings, "ENV", "test", raising=False)
    monkeypatch.setattr(cs.settings, "DAILY_BUDGET_ENFORCED", False, raising=False)
    assert cs.daily_budget_exceeded(ctx["db"], ctx["member_id"]) is False

    monkeypatch.setattr(cs.settings, "ENV", "prod", raising=False)
    assert cs.daily_budget_exceeded(ctx["db"], ctx["member_id"]) is True


# --------------------------------------------------------------------------- #
# QA C4 재검-①(2026-09-23) — 이어하기가 call_date 를 밀면 자정 경계에서 예산이 샌다
# --------------------------------------------------------------------------- #
def test_resume_does_not_move_the_call_into_the_next_days_budget(ctx):
    """자정 직전 시작 → 자정 넘겨 조각2 를 열어도 `call_date` 는 그대로라, 예산 차감
    (누적 total_time)은 **시작한 날**에만 잡힌다.

    ⛔⛔ 이 시험이 막는 회귀: `resume_call` 이 `call_date` 를 조각2 시작 시각으로
      덮어쓰면, 조각1+조각2 누적 `total_time` 전체가 조각2 를 연 **다음 날**로 옮겨가
      시작한 날 예산이 빈 것처럼 보이고(써야 할 만큼 못 막음) 다음 날 예산이 미리
      깎인다(안 써야 할 만큼 막음) — 양방향으로 틀린다.
    """
    start_day = date(2026, 9, 23)
    started_at = datetime(2026, 9, 23, 23, 58, tzinfo=timezone.utc)   # 자정 직전(UTC)
    call = _call(ctx, total_time=300, call_type="expression", status="done", when_utc=started_at)

    # 자정을 넘겨 조각2 를 연다(이어하기).
    got, why = ns.resume_call(ctx["db"], ctx["member_id"], call.call_id, max_fragments=3)
    assert got == call.call_id, why
    ctx["db"].refresh(call)
    refreshed = call.call_date if call.call_date.tzinfo else call.call_date.replace(tzinfo=timezone.utc)
    assert refreshed == started_at, "resume_call 이 call_date 를 덮어썼다 — 그 회귀"

    # 조각2 가 끝나 total_time 이 누적됐다(12차 조각 누적 계약).
    call.total_time = 600
    ctx["db"].commit()

    repo = CallRepository(ctx["db"])
    s0, e0 = cs.local_window_utc(start_day, None, 0)
    s1, e1 = cs.local_window_utc(start_day + timedelta(days=1), None, 0)
    assert repo.sum_total_time_in_window(ctx["member_id"], s0, e0, exclude_call_types=("level_test",)) == 600, \
        "누적 total_time 이 시작한 날 예산에 안 잡혔다"
    assert repo.sum_total_time_in_window(ctx["member_id"], s1, e1, exclude_call_types=("level_test",)) == 0, \
        "call_date 가 다음 날로 옮겨져 다음 날 예산까지 깎였다"


# --------------------------------------------------------------------------- #
# QA C4 재검-③(2026-09-23) — 레벨테스트는 continues_call_id 유무와 무관하게 횟수 검사
# --------------------------------------------------------------------------- #
# WS 라우팅 레벨 회귀는 tests/test_normalcall_ws.py 의
# test_level_test_with_continues_call_id_still_hits_the_count_limit 가 잡는다
# (call_session.py 의 `continues_call_id is None` 가드 제거 — 예산 스위치가 아니라
# 레벨테스트 횟수 축이라 이 파일이 아니라 그 WS 시험 파일이 근거를 들고 있다).
