"""C4(2026-09-23, D4) — 일일 통화 예산(분) 서버 판정.

Free 300s(5분) · Premium 900s(15분), chat·expression·freetalk 합산(레벨테스트 제외).
근거: docs/plans/2026-09-22-프리미엄-자유대화-15분-달력.md(C4)

⭐⭐ C4 재설계(2026-09-23, bt-back A·B·C·D) — "진행 중 조각의 경과 시간 추정"을
  통째로 없앴다. `mark_fragment_ended`(call_session.py, 끊김을 인지한 그 자리)가
  이제 `fragment_ended_at` 과 `total_time` 을 **같은 쓰기**로 확정하므로,
  `sum_total_time_in_window` 는 단순 `SUM(total_time)` 이다(한 사람 한 통화 게이트
  가 애초에 "진행 중" 조각과 새 예산 조회가 동시에 일어나는 상황을 막는다). 남긴 것:
  자정 귀속(call_date 고정) · 한 사람 한 통화(active_ongoing_call_id, 판정은
  fragment_started_at/ended_at 그대로) · 두 시각 컬럼 · 백필 마이그레이션. 앱이
  통째로 죽어 끊김 신호조차 못 오면 540s 절대 백스톱에 기댄다(감수).

시험 목록(문서 그대로):
    - Free 300 소진 → 거절
    - premium 에서 Free 로 쓴 300 이 차감돼 600 남음("결제 직후 10분")
    - 레벨테스트는 차감 안 함
    - 조각2 시작 시 남은 0 이면 거절(WS 레벨은 test_normalcall_ws.py 쪽에서 잡는다)
    - 서머타임 경계(America/New_York) 자정 계산
    - admin 면제 / admin+override 적용
    - active_ongoing_call_id 는 재설계와 무관 — status 덮어쓰기·540s 컷오프 그대로
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


def _call(ctx, *, total_time, call_type="chat", status="done", when_utc=None,
          fragment_started_at=None, fragment_ended_at=None):
    """QA C4 재검-3차(2026-09-23): "진행 중"은 이제 `fragment_started_at`/`fragment_ended_at`
    로만 판정한다(status 무관) — status="ongoing" 으로 진행 중을 흉내내려면
    `fragment_started_at` 을 같이 넘겨야 한다(기본 None = "진행 중 아님").
    """
    when_utc = when_utc or datetime.now(timezone.utc)
    c = Call(member_id=ctx["member_id"], character_id=ctx["cid"], call_date=when_utc,
              total_time=total_time, status=status, call_type=call_type,
              fragment_started_at=fragment_started_at, fragment_ended_at=fragment_ended_at)
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


# --------------------------------------------------------------------------- #
# C4 재설계(2026-09-23, bt-back E) — 조각1 저장 뒤 조각2 가 남은 예산을 정확히 본다
# --------------------------------------------------------------------------- #
def test_fragment_1_ending_then_fragment_2_sees_the_exact_remaining_budget(ctx):
    """⭐⭐ mark_fragment_ended 가 끊김을 인지한 자리에서 total_time 을 확정하는 새
    경로 자체를 부른다(추정이 아니라 이 함수가 진짜 쓰이는 계약). 조각1 이 100초를
    쓰고 끝난 뒤, 조각2 가 시작 전 남은 예산을 정확히(Free 300-100=200) 봐야 한다.
    """
    call = _call(ctx, total_time=None, call_type="expression", status="ongoing")
    ns.mark_fragment_ended(ctx["db"], call.call_id, total_time=100, accumulate=False)  # 조각1 종료
    ctx["db"].refresh(call)
    assert call.total_time == 100

    remaining = cs.daily_budget_s(None) - cs.used_seconds_today(ctx["db"], ctx["member_id"])
    assert remaining == 200, "조각1 종료 직후 조각2 가 볼 남은 예산이 정확해야 한다"

    # 조각2 재개 + 종료(50초 추가) — 조각 누적(accumulate=True).
    got, why = ns.resume_call(ctx["db"], ctx["member_id"], call.call_id, max_fragments=3)
    assert got == call.call_id, why
    ns.mark_fragment_ended(ctx["db"], call.call_id, total_time=50, accumulate=True)
    ctx["db"].refresh(call)
    assert call.total_time == 150, "조각2 의 시간이 조각1 에 누적돼야 한다(덮어쓰기 아님)"

    remaining_after = cs.daily_budget_s(None) - cs.used_seconds_today(ctx["db"], ctx["member_id"])
    assert remaining_after == 150


def test_ongoing_call_with_recorded_total_time_counts_as_in_progress_spend(ctx):
    """아직 저장이 끝나지 않은 ongoing 도 진행 중인 소비다 — 빼면 끊고 바로 또 거는 구멍."""
    _call(ctx, total_time=300, status="ongoing")
    assert cs.used_seconds_today(ctx["db"], ctx["member_id"]) == 300


def test_active_ongoing_call_id_survives_a_delayed_analysis_overwriting_status_to_done(ctx):
    """⭐⭐ QA C4 재검-3차(codex 재현 시나리오, active 축): 위와 같은 상황에서
    `active_ongoing_call_id` 도 이 통화를 계속 "살아있다"고 봐야 한다 — status 가
    done 으로 덮였다고 동시통화 게이트가 뚫리면(다른 세션이 또 열리면) 안 된다.
    """
    started = datetime.now(timezone.utc) - timedelta(seconds=120)
    call = _call(ctx, total_time=180, call_type="expression", status="ongoing",
                 fragment_started_at=started, fragment_ended_at=None)
    call.status = "done"  # 조각1 의 지연된 분석 완료 — status 만 덮어쓴다
    ctx["db"].commit()

    assert cs.active_ongoing_call_id(ctx["db"], ctx["member_id"]) == call.call_id, \
        "지연된 status=done 덮어쓰기로 진행 중인 조각2 가 active 판정에서 사라졌다"


def test_failed_status_still_counts_as_active(ctx):
    """⭐⭐ QA C4 재검-6차: 진행 중 판정 쿼리의 **status 필터 자체를 없앴다** — 분석이
    `failed` 로 덮는 경합(재검-3차의 `done` 재현과 같은 축, 상태값만 다르다)도
    active 판정에서 그대로 잡혀야 한다.

    ⛔ C4 재설계(2026-09-23): "진행 중 조각의 예산 추정"은 삭제됐다(bt-back B) —
      이 시험은 이제 active_ongoing_call_id 축만 확인한다. 예산은 mark_fragment_ended
      가 끊김을 인지한 자리에서 즉시 확정하므로, "아직 안 끝난 조각"의 예산을
      추정할 필요 자체가 없어졌다.
    """
    started = datetime.now(timezone.utc) - timedelta(seconds=120)
    call = _call(ctx, total_time=180, call_type="expression", status="ongoing",
                 fragment_started_at=started, fragment_ended_at=None)
    call.status = "failed"  # 분석 파이프라인이 실패로 덮었다 — status 만 바뀐다
    ctx["db"].commit()

    assert cs.active_ongoing_call_id(ctx["db"], ctx["member_id"]) == call.call_id, \
        "status=failed 로 덮여도 진행 중인 조각은 active 여야 한다"


def test_dead_fragment_is_not_active_past_the_cap(ctx):
    """⭐⭐ QA C4 재검-6차: 종료 표식을 못 남긴 죽은 조각(크래시로 fragment_ended_at 을
    영영 못 찍음)은 경과가 상한(540s)을 넘기면 "살아있다"(active, 동시통화 게이트)
    판정에서 제외한다 — 죽은 행 하나가 그 회원을 영영 통화 못 걸게 잠그면 안 된다.

    ⛔ C4 재설계(2026-09-23): 옛 시험은 "그래도 예산엔 540초를 계상한다"를 같이
      쟀지만, 그 예산-추정 로직은 삭제됐다(bt-back B·D) — 앱이 완전히 죽어 끊김
      신호조차 못 오면 540s 절대 백스톱이 대신 발동해 그 자리에서 mark_fragment_ended
      가 **실제** 경과 시간을 total_time 에 확정한다(백스톱이 안 뜨는 한 이 통화는
      계속 "안 끝난" 상태로 남고, 그동안은 예산 계산 대상이 아니다 — 감수한 설계).
    """
    started = datetime.now(timezone.utc) - timedelta(seconds=600)   # 상한(540)보다 오래됨
    _call(ctx, total_time=None, call_type="expression", status="ongoing",
          fragment_started_at=started, fragment_ended_at=None)

    assert cs.active_ongoing_call_id(ctx["db"], ctx["member_id"]) is None, \
        "540초 넘은 죽은 조각이 여전히 active 로 잡혔다"


def test_backfilled_old_row_is_not_active_and_budgets_only_total_time(ctx):
    """⭐⭐ QA C4 재검-4차: 마이그레이션(30dda365f616)이 옛 행에
    `fragment_started_at=call_date`·`fragment_ended_at=updated_at` 를 백필한다 —
    "진행 중처럼 보이는 옛 행도 죽은 것으로 간주"하므로, 백필된 행은 active 가 아니고
    예산은 `total_time` 만 본다(경과 추정이 안 붙는다).
    """
    call = _call(ctx, total_time=200, status="ongoing")  # 옛 행 흉내: 생성 시 두 컬럼 NULL(오늘 창 안)
    # 마이그레이션이 하는 일 그대로: call_date → fragment_started_at, updated_at → fragment_ended_at.
    call.fragment_started_at = call.call_date
    call.fragment_ended_at = call.updated_at
    ctx["db"].commit()

    assert cs.active_ongoing_call_id(ctx["db"], ctx["member_id"]) is None, \
        "백필된 옛 행이 여전히 active 로 잡혔다(진행 중처럼 보이는 옛 행도 죽은 것으로 봐야 한다)"
    assert cs.used_seconds_today(ctx["db"], ctx["member_id"]) == 200, \
        "백필된 옛 행의 예산이 total_time 이 아닌 다른 값으로 계산됐다"


def test_finalize_call_does_not_overwrite_an_already_stamped_fragment_ended_at(ctx):
    """⭐⭐ QA C4 재검-5차(재재검): `finalize_call` 은 `fragment_ended_at` 이 **이미
    찍혀 있으면 덮지 않는다** — 즉시 표식(`mark_fragment_ended`, 세션 끊김을 인지한
    순간)이 TTL 의 단일 기준이다. `finalize_call` 은 무거운 마무리 저장 뒤(더 늦게)
    불릴 수 있어서, 무조건 덮으면 그 늦은 시각으로 TTL 이 매번 다시 늘어난다.
    """
    from domains.learning.service import normalcall_service as _ns

    call = _call(ctx, total_time=None, call_type="expression", status="ongoing")
    immediate_stamp = datetime.now(timezone.utc) - timedelta(seconds=5)
    call.fragment_ended_at = immediate_stamp
    ctx["db"].commit()

    _ns.finalize_call(ctx["db"], call.call_id, total_time=300, status="analyzing")
    ctx["db"].refresh(call)

    stamped = call.fragment_ended_at if call.fragment_ended_at.tzinfo else \
        call.fragment_ended_at.replace(tzinfo=timezone.utc)
    assert abs((stamped - immediate_stamp).total_seconds()) < 1, \
        "finalize_call 이 이미 찍혀 있던 fragment_ended_at 을 늦은 시각으로 덮어썼다"
    assert call.total_time == 300, "fragment_ended_at 보호와 무관하게 total_time 은 정상 갱신돼야 한다"


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
# C5(2026-09-23) — remaining_budget_s = **하루 잔여**(조각 상한 클램프 없음)
# --------------------------------------------------------------------------- #
def test_remaining_budget_s_is_the_plain_daily_remainder(ctx):
    """Free 300 중 100 사용 → 남은 200(조각 상한 360 과 무관, 그냥 예산-사용)."""
    _call(ctx, total_time=100)
    assert cs.remaining_budget_s(ctx["db"], ctx["member_id"]) == 200


def test_remaining_budget_s_is_not_capped_by_the_fragment_length(ctx):
    """⭐⭐⭐ C5 후속 결함(2026-09-23, bt-back) 회귀 — premium 900 중 0 사용이면 남은
    900 그대로여야 한다(조각 상한 360 으로 잘리면 daily-status 의 budget_s-used_s
    산수가 안 맞는다). 조각 상한 클램프는 call_started 를 만드는 자리에서만 건다."""
    m = ctx["db"].get(Member, ctx["member_id"])
    m.role = "user"
    ctx["db"].commit()
    assert cs.remaining_budget_s(ctx["db"], ctx["member_id"], plan_override="premium") == 900


def test_remaining_budget_s_floors_at_zero_when_over_budget(ctx):
    _call(ctx, total_time=999)
    assert cs.remaining_budget_s(ctx["db"], ctx["member_id"]) == 0


def test_remaining_budget_s_is_none_for_an_exempt_admin(ctx):
    _call(ctx, total_time=100)
    m = ctx["db"].get(Member, ctx["member_id"])
    m.role = "admin"
    ctx["db"].commit()
    assert cs.remaining_budget_s(ctx["db"], ctx["member_id"]) is None


def test_remaining_budget_s_applies_when_an_admin_sends_a_plan_override(ctx):
    """⭐ plan_override 를 보낸 admin 은 면제가 풀리고 그 플랜 예산이 적용된다(개발자 도구)."""
    _call(ctx, total_time=100)
    m = ctx["db"].get(Member, ctx["member_id"])
    m.role = "admin"
    ctx["db"].commit()
    assert cs.remaining_budget_s(ctx["db"], ctx["member_id"], plan_override="free") == 200


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
# R2-a(2026-09-24, 프론트 실기기 QA) — 자정 걸친 조각 체인이 새 날 예산을 안 깎는다
# --------------------------------------------------------------------------- #
# 위 test_resume_does_not_move_the_call_into_the_next_days_budget 이 지키는
# "call_date 는 최초 시작일 고정"은 그대로 맞다 — 문제는 그 고정 때문에, 체인이
# 자정을 넘겨 **다음 날 조각을 이으려 할 때** 그 예산 판정이 이미 쓴 시간을 못 보고
# 새 날 예산이 고스란히 남은 것으로 착각한다는 것이다(premium 저녁~새벽 한 시간에
# 최대 30분). 고친 방향(bt-back 제안 b, «판정 창을 조각 시작일 기준으로») —
# resuming_call_id 를 넘기면 그 통화가 call_date 창 밖이어도 지금까지 누적된
# total_time 을 이 창에 반영한다. call_date 자체는 안 건드린다.
def test_resuming_call_id_pulls_a_midnight_crossing_chains_usage_into_todays_budget(ctx):
    """⛔⛔ 핵심 재현·수정 확인 — 어제 23:50 시작한 900초 체인을 오늘 이으려 하면,
    resuming_call_id 를 넘긴 오늘 창의 SUM 이 그 900초를 반영해야 한다."""
    yesterday_2350 = datetime(2026, 9, 23, 23, 50, tzinfo=timezone.utc)
    call = _call(ctx, total_time=900, call_type="expression", status="analyzing",
                 when_utc=yesterday_2350)

    today = date(2026, 9, 24)
    s, e = cs.local_window_utc(today, None, 0)
    repo = CallRepository(ctx["db"])

    # 옛 동작(조정 없음) — 오늘 SUM 은 그 통화를 못 본다(call_date=어제). 다른 용도
    # (학습 달력 등)는 여전히 이 값을 써야 하므로 기본값은 그대로 0이어야 한다.
    assert repo.sum_total_time_in_window(
        ctx["member_id"], s, e, exclude_call_types=("level_test",)
    ) == 0

    # R2-a 조정 — resuming_call_id 를 넘기면 그 900초가 오늘 창에도 반영된다.
    assert repo.sum_total_time_in_window(
        ctx["member_id"], s, e, exclude_call_types=("level_test",),
        also_include_call_id=call.call_id,
    ) == 900


def test_resuming_call_id_does_not_double_count_a_same_day_chain(ctx):
    """회귀 — 같은 날 안의 체인은 call_date 가 이미 오늘 창 안이라, resuming_call_id
    를 넘겨도 두 번 더해지면 안 된다(이중 계산 방지)."""
    today_noon = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    call = _call(ctx, total_time=300, call_type="expression", status="analyzing",
                 when_utc=today_noon)

    s, e = cs.local_window_utc(date(2026, 9, 24), None, 0)
    repo = CallRepository(ctx["db"])
    without = repo.sum_total_time_in_window(
        ctx["member_id"], s, e, exclude_call_types=("level_test",)
    )
    with_adjust = repo.sum_total_time_in_window(
        ctx["member_id"], s, e, exclude_call_types=("level_test",),
        also_include_call_id=call.call_id,
    )
    assert without == 300 and with_adjust == 300, "같은 날 조각은 이미 SUM 에 있으니 또 더하면 안 된다"


def test_resuming_call_id_is_a_noop_for_a_call_that_does_not_cross_midnight(ctx):
    """회귀 — 자정을 안 걸치는 통화는 resuming_call_id 유무와 무관하게 결과가 같다."""
    call = _call(ctx, total_time=150, call_type="chat", status="done")  # when_utc=지금
    now = datetime.now(timezone.utc)
    s, e = cs.local_window_utc(now.date(), None, 0)
    repo = CallRepository(ctx["db"])
    assert repo.sum_total_time_in_window(ctx["member_id"], s, e) == \
        repo.sum_total_time_in_window(ctx["member_id"], s, e, also_include_call_id=call.call_id) == 150


def test_resuming_call_id_ignores_another_members_call(ctx):
    """방어 — resuming_call_id 가 남의 통화면(위조·오류) 무시한다(자기 자신에게만 영향)."""
    other = Member(language="en", onboarding_completed=True, auth_user_id="auth-budget-other")
    ctx["db"].add(other); ctx["db"].commit()
    other_call = Call(member_id=other.member_id, character_id=ctx["cid"],
                      call_date=datetime(2026, 9, 23, 23, 50, tzinfo=timezone.utc),
                      total_time=900, status="analyzing", call_type="expression")
    ctx["db"].add(other_call); ctx["db"].commit()

    s, e = cs.local_window_utc(date(2026, 9, 24), None, 0)
    repo = CallRepository(ctx["db"])
    assert repo.sum_total_time_in_window(
        ctx["member_id"], s, e, also_include_call_id=other_call.call_id,
    ) == 0, "남의 통화 total_time 이 내 예산 판정에 반영됐다"


def test_daily_budget_exceeded_and_remaining_honor_resuming_call_id(ctx, monkeypatch):
    """같은 시나리오를 실제 게이트 함수로 — 어제 23:50 시작한 900초(premium 한도와
    같은 값) 체인을 '오늘' 이으려 하면 예산이 이미 다 찼다고 거절해야 한다."""
    monkeypatch.setattr(
        "domains.commerce.service.entitlements.effective_plan",
        lambda db, member_id: "premium",
    )
    fixed_today = date(2026, 9, 24)
    monkeypatch.setattr(
        cs, "local_window_utc",
        lambda local_date, tz, tz_offset_min: cs.daily_window_utc(local_date or fixed_today, tz_offset_min or 0),
    )
    yesterday_2350 = datetime(2026, 9, 23, 23, 50, tzinfo=timezone.utc)
    call = _call(ctx, total_time=900, call_type="expression", status="analyzing",
                 when_utc=yesterday_2350)

    # resuming_call_id 없이(옛 동작) — 오늘 예산이 고스란히 남은 것으로 잘못 판정.
    assert cs.daily_budget_exceeded(ctx["db"], ctx["member_id"]) is False
    assert cs.remaining_budget_s(ctx["db"], ctx["member_id"]) == 900

    # resuming_call_id 를 넘기면(수정) — 이미 다 썼다고 정확히 거절.
    assert cs.daily_budget_exceeded(
        ctx["db"], ctx["member_id"], resuming_call_id=call.call_id,
    ) is True
    assert cs.remaining_budget_s(
        ctx["db"], ctx["member_id"], resuming_call_id=call.call_id,
    ) == 0


# --------------------------------------------------------------------------- #
# QA C4 재검-③(2026-09-23) — 레벨테스트는 continues_call_id 유무와 무관하게 횟수 검사
# --------------------------------------------------------------------------- #
# WS 라우팅 레벨 회귀는 tests/test_normalcall_ws.py 의
# test_level_test_with_continues_call_id_still_hits_the_count_limit 가 잡는다
# (call_session.py 의 `continues_call_id is None` 가드 제거 — 예산 스위치가 아니라
# 레벨테스트 횟수 축이라 이 파일이 아니라 그 WS 시험 파일이 근거를 들고 있다).
