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
    """⛔ 표를 캐스케이드가 따로 갖지 않는다 — 갈리면 두 경로의 과금 규칙이 달라진다.

    ⭐ 2026-08-19 재편: 길이는 **플랜 무관 상수**(조각 6분)가 됐고, 플랜은 조각 수를
      가른다. 캐스케이드는 조각 개념이 없으므로 길이만 같으면 된다.
    """
    assert call_service.CALL_DURATION_S_BY_PLAN[None] == call_service.CALL_FRAGMENT_S
    assert call_service.CALL_DURATION_S_BY_PLAN["pro"] == call_service.CALL_FRAGMENT_S
    assert call_service.FREE_CALL_DURATION_S == call_service.CALL_FRAGMENT_S


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
    # ⚠ 통지는 시계가 하지 않는다(아래 시험) — **사유만** 남기고 끝낸다.
    #   `call_id` 는 종료 마감 뒤에 확정되므로 통지도 그 뒤에서 한 번만 나간다.
    assert session._end_reason == "duration"


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


def test_the_clock_neither_finalizes_nor_notifies():
    """⛔ 마감(usage·통화행)도 종료 통지도 **시계가 하지 않는다**.

    · 마감이 두 곳이면 한 통화가 **두 번 저장**된다.
    · 통지가 두 곳이면 **0회 또는 2회**가 되기 쉽고, `call_id` 가 확정되기 **전에** 나가면
      빈 값이 실린다(예전 `str(... or "")` 가 정확히 그 사고였다).
    """
    import ast
    import inspect
    import textwrap

    # ⚠ 소스 **문자열**로 보면 주석에 걸린다(그 사고를 이 프로젝트에서 이미 두 번 냈다) —
    #   여기서는 이 함수가 **실제로 부르는 것**만 본다.
    tree = ast.parse(textwrap.dedent(inspect.getsource(cs.CascadeSession._watch_call_clock)))
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    constructed = {n.func.id for n in ast.walk(tree)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_finalize_call" not in called, called
    assert "save_call_usage" not in called
    assert not [c for c in constructed if "CallEnded" in c], "시계가 종료 통지까지 한다"


# ── 🔴 종료 통지는 **모든 경로**에서 정확히 1회(2026-08-12 프론트 보고) ────
@pytest.mark.parametrize("path,reason", [("client", "client"),
                                         ("duration", "duration"),
                                         ("backstop", "backstop")])
@pytest.mark.asyncio
async def test_call_ended_fires_once_on_every_path(monkeypatch, path, reason):
    """⛔ 예전엔 **시간 만료 때만** 나갔다. 사용자가 끊으면 안 나가서, 클라가 `GET /calls` 를
    **5회×600ms 폴링**해 call_id 를 되짚고 있었다 — 3초를 헛돌고, 그 사이 다른 통화가 생기면
    **엉뚱한 id 를 집는다.**

    세 경로 각각에서 **정확히 1회**, **실제 call_id** 를 싣고 나가야 한다(0회도 2회도 아님).
    """
    import core.stt as stt_mod

    monkeypatch.setattr(stt_mod.settings, "STT_V2_FAKE", True)
    monkeypatch.setattr(cs.settings, "CASCADE_GREETING", False)
    monkeypatch.setattr(cs.settings, "CASCADE_FAREWELL_GRACE_S", 0.1)
    stt_mod.get_speech_v2_client.cache_clear()

    if path == "duration":
        monkeypatch.setattr(cs.settings, "NORMAL_CALL_DURATION_S", 0.1)
    elif path == "backstop":
        # 시계가 멈춘 상황을 만든다 — 그게 백스톱이 존재하는 이유다.
        async def _stuck(self):
            await asyncio.sleep(60)

        monkeypatch.setattr(cs.CascadeSession, "_watch_call_clock", _stuck)
        monkeypatch.setattr(cs.settings, "NORMAL_CALL_DURATION_S", 0.05)
        monkeypatch.setattr(cs.settings, "CASCADE_SESSION_MAX_S", 0.3)

    class _Transport:
        def __init__(self) -> None:
            self.events: list[dict] = []
            self._sent = False

        async def send_event(self, event: dict) -> None:
            self.events.append(event)

        async def send_audio(self, frame: bytes) -> None:
            return None

        async def receive(self):
            if not self._sent:
                self._sent = True
                return cs.CascadeInbound(kind="control",
                                         control={"type": "start", "sampleRate": 16000})
            if path == "client":
                return cs.CascadeInbound(kind="control", control={"type": "stop"})
            await asyncio.sleep(30)

    transport = _Transport()
    session = cs.CascadeSession(transport)
    session._call_id = 77          # 통화 행이 이미 있다고 본다(마감은 팩토리 없이 건너뛴다)
    await asyncio.wait_for(session.run(), timeout=5)

    ended = [e for e in transport.events if e.get("type") == "call_ended"]
    assert len(ended) == 1, transport.events
    assert ended[0]["call_id"] == "77", ended
    assert ended[0]["reason"] == reason, ended


@pytest.mark.asyncio
async def test_call_ended_carries_null_not_an_empty_string(monkeypatch):
    """⛔ 빈 문자열 금지 — 클라가 `""` 를 **유효한 id 로 착각**한다(프론트 지적)."""
    import json

    from domains.learning.realtime.cascade_protocol import (
        CascadeCallEnded,
        cascade_server_adapter,
    )

    frame = json.loads(cascade_server_adapter.dump_json(
        CascadeCallEnded(call_id=None, reason="client")).decode())
    assert frame["call_id"] is None, frame
