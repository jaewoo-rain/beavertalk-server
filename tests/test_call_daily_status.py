"""CallService.daily_status — '오늘 통화함' 파생 체크 (외부 의존 0, 인메모리 sqlite).

검증:
    - 로컬 하루 안 **학습자가 말한** done 통화 → called_today True.
    - 학습자 발화 0건 / status ongoing / 어제 통화 → False.
    - 콜타입 분리: 레벨테스트는 called_today 를 켜지 않는다(한도가 따로다).
    - 타임존 경계: 같은 통화라도 클라 로컬 날짜·오프셋에 따라 오늘/어제 갈림.
    - date 형식·tz_offset 범위 오류 → 422.
member 컬럼/일일 초기화 없이 call 테이블에서 EXISTS 파생임을 확인한다.

⚠ 옛 기준(total_time >= 10초)은 폐기됐다. 실측에서 마이크가 안 열린 통화가 10초를
   넘겨 하루를 소모하고 있었다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.service.call_service import CallService


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
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-d")
    db.add(m); db.flush()
    return {"db": db, "member_id": m.member_id, "cid": ch.character_id}


def _call(ctx, *, when_utc: datetime, total_time=60, status="done",
          spoke=True, call_type="normal"):
    """통화 1건. spoke=True 면 학습자 발화 행을 함께 넣는다(성립 조건).

    선톡은 role='beaver' 라 성립에 안 쓰이므로, 비버 발화만 있는 통화 = spoke False.
    """
    c = Call(member_id=ctx["member_id"], character_id=ctx["cid"],
             call_date=when_utc, total_time=total_time, status=status,
             call_type=call_type)
    ctx["db"].add(c); ctx["db"].flush()
    ctx["db"].add(CallRawData(call_id=c.call_id, role="beaver", turn_index=0,
                              content="안녕! 오늘 뭐 할까?"))  # 선톡 — 성립에 안 쓰임
    if spoke:
        ctx["db"].add(CallRawData(call_id=c.call_id, role="user", turn_index=1,
                                  content="안녕하세요"))
    ctx["db"].commit()
    return c


def _status(ctx, date, tz_offset):
    return CallService(ctx["db"]).daily_status(ctx["member_id"], date, tz_offset)


# --------------------------------------------------------------------------- #
def test_called_today_kst(ctx):
    # KST(+540) 2026-07-17 10:00 = UTC 2026-07-17 01:00 → 로컬 07-17
    _call(ctx, when_utc=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc), total_time=30)
    # ⭐ 2026-08-20: 판정 필드가 늘었다(can_call_* · max_fragments).
    #   ⚠ can_call_* 이 True 인 건 ENV != "prod" 라 서버가 실제로 안 막기 때문이다 —
    #     아래 test_can_call_mirrors_the_server_refusal 이 그 계약을 따로 잡는다.
    assert _status(ctx, "2026-07-17", 540) == {
        "date": "2026-07-17", "called_today": True, "level_test_today": False,
        "can_call_normal": True, "can_call_level_test": True, "max_fragments": 1}
    # 같은 통화, 다른 로컬 날짜로 물으면 False
    assert _status(ctx, "2026-07-16", 540)["called_today"] is False


def test_call_without_user_speech_excluded(ctx):
    """★ 학습자가 한마디도 안 한 통화는 하루를 소모하지 않는다.

    실측: normal 405건 중 205건이 발화 0건이고 그중 44건이 10초를 넘겼다(최장 324초).
    마이크가 안 열렸거나 듣기만 한 통화가 한도를 깎으면 안 된다.
    """
    _call(ctx, when_utc=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc),
          total_time=300, spoke=False)
    assert _status(ctx, "2026-07-17", 540)["called_today"] is False


def test_short_call_counts_when_user_spoke(ctx):
    """반대로, 짧아도 학습자가 말했으면 성립한다(옛 10초 기준 폐기)."""
    _call(ctx, when_utc=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc),
          total_time=3, spoke=True)
    assert _status(ctx, "2026-07-17", 540)["called_today"] is True


def test_level_test_does_not_consume_normal(ctx):
    """★ 레벨테스트는 일반 통화 한도를 깎지 않는다(콜타입 분리)."""
    _call(ctx, when_utc=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc),
          call_type="level_test")
    s = _status(ctx, "2026-07-17", 540)
    assert s["called_today"] is False
    assert s["level_test_today"] is True


def test_normal_does_not_consume_level_test(ctx):
    _call(ctx, when_utc=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc),
          call_type="normal")
    s = _status(ctx, "2026-07-17", 540)
    assert s["called_today"] is True
    assert s["level_test_today"] is False


def test_ongoing_excluded(ctx):
    _call(ctx, when_utc=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc), total_time=None, status="ongoing")
    assert _status(ctx, "2026-07-17", 540)["called_today"] is False


def test_analyzing_counts(ctx):
    _call(ctx, when_utc=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc), total_time=20, status="analyzing")
    assert _status(ctx, "2026-07-17", 540)["called_today"] is True


def test_yesterday_excluded(ctx):
    # UTC 07-16 03:00 = KST 07-16 12:00 → 로컬 07-16(어제)
    _call(ctx, when_utc=datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc), total_time=30)
    assert _status(ctx, "2026-07-17", 540)["called_today"] is False


def test_tz_boundary_split(ctx):
    # UTC 07-16 20:00 → KST(+540) 07-17 05:00(오늘) / UTC(0) 07-16(어제)
    _call(ctx, when_utc=datetime(2026, 7, 16, 20, 0, tzinfo=timezone.utc), total_time=30)
    assert _status(ctx, "2026-07-17", 540)["called_today"] is True    # KST 기준 오늘
    assert _status(ctx, "2026-07-17", 0)["called_today"] is False      # UTC 기준 어제


def test_no_call_false(ctx):
    assert _status(ctx, "2026-07-17", 540)["called_today"] is False


def test_bad_date_422(ctx):
    with pytest.raises(HTTPException) as e:
        _status(ctx, "2026/07/17", 540)
    assert e.value.status_code == 422


def test_bad_offset_422(ctx):
    with pytest.raises(HTTPException) as e:
        _status(ctx, "2026-07-17", 9999)
    assert e.value.status_code == 422


# --------------------------------------------------------------------------- #
# ⭐ "더 할 수 있나" — 서버 거절과 같은 답을 내는가 (2026-08-20)
# --------------------------------------------------------------------------- #
def test_can_call_mirrors_the_server_refusal(ctx, monkeypatch):
    """⛔⛔ **화면 배지와 서버 거절이 갈리면 안 된다.**

    `called_today` 는 사실이고 `can_call_normal` 은 판정이다 — 같은 사실에서 Free 는
    못 하고 Pro 는 할 수 있으므로 결론이 반대로 갈린다. 그 조합을 클라에 맡기면
    판정이 두 군데가 되고, 어긋나는 순간 "배지는 된다는데 서버가 거절"이 난다.
    ⇒ 이 시험은 **서버 거절 함수와 응답이 같은 답인지**만 본다. 값을 박아두지 않는다.

    ⚠ ENV != "prod" 면 서버가 아무도 안 막으므로 can_call_* 은 항상 True 다. 그것도
      **같은 답**이므로 이 시험은 그대로 통과한다 — 그게 이 계약의 요점이다.
    """
    from domains.learning.service import call_service as cs

    # ⛔⛔ **`can_call_*` 은 요청한 `date` 가 아니라 서버의 '지금'을 본다.**
    #   is_daily_limit_reached 가 datetime.now() 로 창을 잡기 때문이다. 처음 이 시험을
    #   과거 날짜(2026-07-17)로 썼더니 prod 에서도 안 막혔다 — 그날 통화는 오늘 창 밖이다.
    #   ⭐ 이건 버그가 아니라 **맞는 동작**이다: 서버 거절은 통화를 거는 **그 순간** 일어나므로
    #     can_call_* 이 '지금'을 봐야 거절과 같은 답이 된다. date 를 따르게 만들면 오히려
    #     "어제 날짜로 물으면 된다고 한다"가 되어 두 값이 갈린다.
    #   ⇒ 그래서 이 시험은 **오늘 통화**를 심는다. date 축은 called_today 가 소유한다.
    now = datetime.now(timezone.utc)
    _call(ctx, when_utc=now, total_time=30)
    today_local = (now + timedelta(minutes=540)).date().isoformat()

    out = _status(ctx, today_local, 540)
    for call_type, key in (("normal", "can_call_normal"),
                           ("level_test", "can_call_level_test")):
        refused = cs.is_daily_limit_reached(ctx["db"], ctx["member_id"], call_type, 540)
        assert out[key] is (not refused), (
            "%s 가 서버 거절과 어긋난다 — 배지와 거절이 갈리는 그 버그다" % key
        )

    # ⛔ prod 로 올리면 Free 는 실제로 막혀야 한다(한도 배선이 살아 있는지 확인).
    monkeypatch.setattr(cs.settings, "ENV", "prod", raising=False)
    blocked = _status(ctx, today_local, 540)
    assert blocked["called_today"] is True
    assert blocked["can_call_normal"] is False, "prod 에서 Free 가 두 번째 통화를 못 막는다"
    # 레벨테스트는 콜타입이 달라 아직 남아 있다(한도를 따로 센다 — 위 주석 참조).
    assert blocked["can_call_level_test"] is True


def test_max_fragments_is_the_same_source_as_resume_status(ctx):
    """⚠ 시작 화면과 연장 화면이 다른 말을 하면 안 된다.

    `daily-status.max_fragments` 와 `resume-status.max_fragments` 는 **같은 함수**
    (call_fragments_for_member)에서 와야 한다. 한쪽만 고치면 "시작할 땐 3조각이라더니
    연장하려니 1조각" 이 된다.
    """
    from domains.learning.service import call_service as cs

    out = _status(ctx, "2026-07-17", 540)
    assert out["max_fragments"] == cs.call_fragments_for_member(
        ctx["db"], ctx["member_id"]
    )
    assert out["max_fragments"] == cs.FREE_CALL_FRAGMENTS, "플랜 없는 회원은 Free(1)"


def test_daily_limit_switch_is_independent_of_env(ctx, monkeypatch):
    """⭐ **한도만 따로 켜는 스위치**(2026-08-20 사장님 지시).

    ⛔ 왜 ENV 로 안 하나: `ENV=prod` 는 한도만 켜는 값이 아니다 — dev 데모 라우트
      (main.py `/__levelcalldemo`·`/__enginedemo`)와 통화 prod 가드도 같이 켠다.
      프론트가 한도 UI 를 검증하려면 **한도만** 켜야 했다.
    ⛔⛔ 켜면 Free 는 하루 1통화에서 잠긴다. 켤 위치를 고를 때 그 대가를 보라.
    """
    from domains.learning.service import call_service as cs

    now = datetime.now(timezone.utc)
    _call(ctx, when_utc=now, total_time=30)
    today_local = (now + timedelta(minutes=540)).date().isoformat()

    # ① 기본값(꺼짐) + ENV=test → 안 막는다
    monkeypatch.setattr(cs.settings, "ENV", "test", raising=False)
    monkeypatch.setattr(cs.settings, "DAILY_LIMIT_ENFORCED", False, raising=False)
    assert _status(ctx, today_local, 540)["can_call_normal"] is True

    # ② 스위치만 켜면 ENV 가 test 여도 막는다 — 이게 이 변경의 요점이다
    monkeypatch.setattr(cs.settings, "DAILY_LIMIT_ENFORCED", True, raising=False)
    assert _status(ctx, today_local, 540)["can_call_normal"] is False,         "스위치를 켰는데 안 막는다 — ENV 축과 분리가 안 된 것이다"

    # ③ ⚠ prod 는 스위치와 무관하게 계속 막는다(회귀 방어).
    #    실서비스에서 플래그 하나 안 켰다고 한도가 풀리면 그게 사고다.
    monkeypatch.setattr(cs.settings, "ENV", "prod", raising=False)
    monkeypatch.setattr(cs.settings, "DAILY_LIMIT_ENFORCED", False, raising=False)
    assert _status(ctx, today_local, 540)["can_call_normal"] is False,         "prod 에서 한도가 풀렸다"


def test_admin_is_exempt_from_the_daily_limit(ctx, monkeypatch):
    """⭐ **admin 은 한도 면제**(2026-08-20 사장님 지시 "특정 계정은 무제한").

    ⛔ 새 축을 안 만들었다 — `member.role`(user|admin)이 이미 있고 이미 특권 축이다.
    ⛔ 왜 구독에 Max 를 꽂지 않았나: 그러면 구독 상태가 active_max 가 되어 **정작 Free
      화면·Free 한도 UI 를 본인이 못 보게 된다.** role 은 구독을 안 건드리므로 화면은
      Free 그대로이고 한도만 안 걸린다 — 테스트용으로 원하는 게 정확히 그것이다.
      이 시험이 그 성질(구독은 그대로 Free)까지 같이 잡는다.
    """
    from domains.account.models.member import Member
    from domains.commerce.service import entitlements
    from domains.learning.service import call_service as cs

    now = datetime.now(timezone.utc)
    _call(ctx, when_utc=now, total_time=30)
    today_local = (now + timedelta(minutes=540)).date().isoformat()
    monkeypatch.setattr(cs.settings, "DAILY_LIMIT_ENFORCED", True, raising=False)

    # ① 일반 회원(user)은 막힌다
    assert _status(ctx, today_local, 540)["can_call_normal"] is False

    # ② admin 으로 올리면 안 막힌다
    m = ctx["db"].get(Member, ctx["member_id"])
    m.role = "admin"
    ctx["db"].commit()
    assert _status(ctx, today_local, 540)["can_call_normal"] is True,         "admin 이 한도에 걸렸다"
    assert _status(ctx, today_local, 540)["can_call_level_test"] is True

    # ③ ⭐ 그런데 **구독은 여전히 Free** 다 — 화면은 Free 그대로여야 한다.
    #    (Max 를 꽂는 방법이었다면 여기가 "max" 가 되어 Free UI 를 못 본다)
    assert entitlements.effective_plan(ctx["db"], ctx["member_id"]) is None

    # ④ ⛔⛔ **조각은 안 늘어난다.** 사장님 확인: "1통화 5분씩 3번까지는 동일한데
    #    하루에 여러번 통화 가능하게 해달라는거야." ⇒ 면제는 **횟수 축에만** 건다.
    #    한 통화의 모양이 admin 만 달라지면 실사용을 재현하지 못한다.
    #    ⚠ 이 단언이 깨졌다면 누군가 call_fragments_for_member 에 admin 특례를 넣은 것이다.
    #      "빠뜨린 것"이 아니라 **의도된 비대칭**이다 — 넣지 마라.
    assert _status(ctx, today_local, 540)["max_fragments"] == cs.FREE_CALL_FRAGMENTS
    assert cs.call_fragments_for_member(ctx["db"], ctx["member_id"])         == cs.FREE_CALL_FRAGMENTS

    # ⑤ 롤을 되돌리면 다시 막힌다 — 면제가 role 에만 걸려 있다는 확인
    m.role = "user"
    ctx["db"].commit()
    assert _status(ctx, today_local, 540)["can_call_normal"] is False


def test_paid_plans_are_also_one_call_a_day(ctx, monkeypatch):
    """⭐⭐ **플랜이 가르는 것은 횟수가 아니라 조각 수다**(2026-08-19 결정 반영).

    결정 원문: *"free는 5분 1통화가 제한이고, pro랑 max는 체인으로 15분 연달아서가
    1통화로 할게."* ⇒ 체인 전체가 '1통화'다.

        Free      하루 1통화 × 조각 1개
        Pro·Max   하루 1통화 × 조각 3개

    ⛔ 이 표만 옛 계약(무제한)에 멈춰 있었다 — 379d654 가 "무제한"으로 박은 뒤 길이
      재편(77ed775)도 조각 재편(08-19)도 이 표를 안 건드렸다. **길이 축만 두 번 고치고
      횟수 축을 놔둔 것**이다.
    ⚠ 앱 문구("Unlimited calls" 10군데)는 프론트가 고친다 — 서버는 서버만 본다.
    """
    from domains.learning.service import call_service as cs

    for plan in (None, "pro", "max"):
        limits = cs.DAILY_CALL_LIMIT_BY_PLAN[plan]
        assert limits.get("normal") == 1, "플랜 %r 이 무제한으로 돌아갔다" % plan
        assert limits.get("level_test") == 1, "플랜 %r 레벨테스트가 무제한이다" % plan

    # ⚠ 조각 수는 반대로 **갈려 있어야** 한다 — 그게 플랜의 차별점이다.
    assert cs.CALL_FRAGMENTS_BY_PLAN[None] == 1
    assert cs.CALL_FRAGMENTS_BY_PLAN["pro"] == 3
    assert cs.CALL_FRAGMENTS_BY_PLAN["max"] == 3
