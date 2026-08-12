"""플랜별 통화 시간 + **작별 후 종료** — Live 의 배관을 그대로 쓴다.

⛔ 지금까지 캐스케이드엔 이 배관이 **통째로 없었다.** 있는 건 절대 백스톱(20분)뿐인데
그건 "펌프가 멈췄을 때 죽이는" 방어선이지 정상 종료가 아니다. 그대로 앱에 붙이면:
  · **Free 회원이 20분씩** 통화한다 → 과금 규칙이 안 먹고 원가가 샌다
  · 5분·15분에 **작별 인사 없이 뚝 끊긴다**

여기서 고정하는 성질:
  ① 플랜별로 길이가 다르다(Free 5분 / Pro·Max 15분) — **Live 와 같은 함수**가 정한다
  ② 시간이 되면 **작별을 먼저** 하고 닫는다(뚝 끊지 않는다)
  ③ 플랜 조회가 실패하면 **Free 로 떨어진다**(모르면 짧은 쪽이 원가에 안전하다)
  ④ ⛔ **절대 백스톱이 정상 종료보다 먼저 오지 않는다**(둘은 다른 층이다)
  ⑤ 종료 뒤 usage·통화행이 마감된다(이번에 붙인 경로 그대로)
"""

import asyncio

import pytest

import domains.learning.realtime.cascade_session as cs
from domains.learning.service import call_service


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send_event(self, event: dict) -> None:
        self.events.append(event)

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def receive(self):
        raise AssertionError("쓰지 않는다")


def _session(monkeypatch, *, plan_duration=None, fail=False) -> cs.CascadeSession:
    async def _run_db(factory, fn):
        return fn(object())

    monkeypatch.setattr(cs.svc, "run_db", _run_db)
    monkeypatch.setattr(cs.svc, "resolve_call_character", lambda *a: 3)
    monkeypatch.setattr(cs.svc, "load_call_setup", lambda *a, **k: {
        "role": "비버", "personality": "밝다", "voice": "Fenrir", "locale": "en",
    })
    monkeypatch.setattr(cs.svc, "create_call", lambda *a, **k: 5)

    def _duration(_db, member_id):
        if fail:
            raise RuntimeError("DB 다운")
        return plan_duration

    monkeypatch.setattr(cs.call_service, "call_duration_s_for_member", _duration)
    return cs.CascadeSession(_Sink(), object(), session_factory=object(),
                             member_id=42, member_target_language="ko")


# ── ① 플랜별 길이 ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("plan_s", [300.0, 900.0])
async def test_the_duration_comes_from_the_plan(monkeypatch, plan_s):
    """⭐ Free 5분 / Pro·Max 15분 — **Live 와 같은 함수**가 정한다."""
    monkeypatch.setattr(cs.settings, "NORMAL_CALL_DURATION_S", None)
    session = _session(monkeypatch, plan_duration=plan_s)
    await session._load_call_context()
    assert session._call_duration_s == plan_s


def test_the_plan_table_is_the_live_one():
    """⛔ 표를 캐스케이드가 따로 갖지 않는다 — 갈리면 두 경로의 과금 규칙이 달라진다."""
    assert call_service.CALL_DURATION_S_BY_PLAN[None] == 300.0
    assert call_service.CALL_DURATION_S_BY_PLAN["pro"] == 900.0
    assert call_service.FREE_CALL_DURATION_S == 300.0


@pytest.mark.asyncio
async def test_the_env_override_wins_like_live(monkeypatch):
    """dev 탈출구 — Live 와 같은 우선순위(env 강제값 > 플랜). DB 없이도 먹어야 한다."""
    monkeypatch.setattr(cs.settings, "NORMAL_CALL_DURATION_S", 7.0)
    session = cs.CascadeSession(_Sink(), object())     # 회원·DB 없음(데모 경로)
    await session._load_call_context()
    assert session._call_duration_s == 7.0


# ── ③ 실패는 Free 로 ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_plan_lookup_failure_falls_back_to_free(monkeypatch, caplog):
    """⛔ 모르면 **짧은 쪽**이다 — 길게 줬다 원가가 새는 것보다 낫다(R5)."""
    import logging

    monkeypatch.setattr(cs.settings, "NORMAL_CALL_DURATION_S", None)
    session = _session(monkeypatch, fail=True)
    with caplog.at_level(logging.WARNING):
        await session._load_call_context()
    assert session._call_duration_s == call_service.FREE_CALL_DURATION_S
    assert any("Free" in r.getMessage() for r in caplog.records), caplog.text


# ── ④ 백스톱은 항상 뒤 ─────────────────────────────────────────────────────
@pytest.mark.parametrize("duration_s", [300.0, 900.0, 1800.0])
def test_the_backstop_never_precedes_the_normal_close(monkeypatch, duration_s):
    """⛔⛔ 백스톱이 먼저 오면 **작별을 못 하고 뚝 끊긴다** — 이 시계를 넣은 목적이 깨진다.

    ⚠ env 로 통화 길이를 20분(백스톱 기본값) 넘게 강제할 수 있으므로 상수 비교로는 부족하다.
    """
    session = cs.CascadeSession(_Sink(), object())
    session._call_duration_s = duration_s
    assert session._backstop_s() > duration_s
    assert session._backstop_s() >= settings_max()


def settings_max() -> float:
    return float(cs.settings.CASCADE_SESSION_MAX_S)


# ── ②⑤ 작별 후 종료 ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_time_up_speaks_a_farewell_before_closing(monkeypatch):
    """⭐ **뚝 끊지 않는다** — 작별 대답을 만들고, 그게 끝난 뒤에 call_ended 를 낸다."""
    monkeypatch.setattr(cs.settings, "CASCADE_FAREWELL_GRACE_S", 1.0)
    said: list[str] = []

    async def _fake_reply(self, user_text, is_greeting=False):
        said.append(user_text)

    monkeypatch.setattr(cs.CascadeSession, "_start_reply", _fake_reply)
    session = cs.CascadeSession(_Sink(), object())
    session._call_duration_s = 0.05
    session._call_id = 5

    with pytest.raises(cs._Stop):
        await session._watch_call_clock()

    assert said, "작별을 시키지 않고 닫았다"
    # ⛔ 문구는 **Live 의 것**이어야 한다(두 경로의 마무리가 갈리면 안 된다).
    assert said[0] == session._close_seed()
    assert "작별" in said[0] and "질문으로 끝내지 마라" in said[0]
    ended = [e for e in session.transport.events if e.get("type") == "call_ended"]
    assert ended and ended[0]["reason"] == "duration", session.transport.events


@pytest.mark.asyncio
async def test_the_farewell_is_not_started_twice(monkeypatch):
    """작별을 두 번 하면 비버가 두 번 인사한다."""
    monkeypatch.setattr(cs.settings, "CASCADE_FAREWELL_GRACE_S", 0.2)
    count = {"n": 0}

    async def _fake_reply(self, user_text, is_greeting=False):
        count["n"] += 1

    monkeypatch.setattr(cs.CascadeSession, "_start_reply", _fake_reply)
    session = cs.CascadeSession(_Sink(), object())
    session._call_duration_s = 0.05
    session._farewell_started = True          # 이미 시작한 상태
    with pytest.raises(cs._Stop):
        await session._watch_call_clock()
    assert count["n"] == 0


@pytest.mark.asyncio
async def test_the_clock_waits_for_a_reply_in_flight(monkeypatch):
    """⚠ 비버가 말하는 중이면 **그 턴이 끝난 뒤**에 작별한다(겹쳐 말하면 I1 위반)."""
    monkeypatch.setattr(cs.settings, "CASCADE_FAREWELL_GRACE_S", 2.0)
    order: list[str] = []

    async def _busy():
        await asyncio.sleep(0.3)
        order.append("앞 대답 끝")

    async def _fake_reply(self, user_text, is_greeting=False):
        order.append("작별")

    monkeypatch.setattr(cs.CascadeSession, "_start_reply", _fake_reply)
    session = cs.CascadeSession(_Sink(), object())
    session._call_duration_s = 0.05
    session._reply_task = asyncio.create_task(_busy())
    with pytest.raises(cs._Stop):
        await session._watch_call_clock()
    assert order == ["앞 대답 끝", "작별"], order


def test_the_clock_does_not_finalize_the_call_itself():
    """⛔ 마감(usage·통화행)은 `run()` 의 finally 가 한다 — 두 곳에서 하면 **두 번 저장**된다."""
    import inspect

    src = inspect.getsource(cs.CascadeSession._watch_call_clock)
    assert "_finalize_call" not in src, src
    assert "save_call_usage" not in src
