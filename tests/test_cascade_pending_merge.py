"""밀린 발화는 **합쳐서 한 번만** 답한다 — 2026-08-12 사장님 실통화(call 937) 회귀.

전사가 증상을 그대로 보여줬다. 사용자가 비버 말 위로 두 마디를 했고, 비버가 **연달아 두 번**
답했다:

    u3  "안녕하세요."      → 대기열
    u4  "여보세요?"        → 대기열(⛔ 예전 구현은 여기서 u3 을 **덮어썼다**)
    b4  (u? 에 대한 답)
    b5  (또 답)            ← 사람이라면 두 마디를 한 번에 받고 **한 번** 답한다

두 가지가 동시에 틀려 있었다:
  ① 대기열이 **문자열 하나**여서 뒤에 온 발화가 앞 발화를 조용히 덮었다(앞말 소실).
  ② 그런데도 소비는 하나씩이라, 두 번 답하는 경로가 남아 있었다.

사장님 결정은 **A(합친다)** 다 — 앞말을 버리는 B 안이 아니다.

여기서 고정하는 성질:
  ① 대기열 2건 → 대답 **1회**(두 말이 **둘 다** 들어간다)
  ② 대기열 3건 → 대답 **1회**
  ③ 대기열 1건 → **기존과 완전히 동일**(문자열이 그대로, 가공 없음)
  ④ **중복 응답 0건** — 꺼낸 뒤 다시 드레인해도 아무 일도 안 일어난다
  ⑤ 대기 시간(`_reply_queued_ms`)은 **첫 발화** 기준이다(뒤엣것으로 갱신하면 지연을 못 본다)
"""

import asyncio

import pytest

from domains.learning.realtime.cascade_session import CascadeInbound, CascadeSession, TurnState


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send_event(self, event: dict) -> None:
        self.events.append(event)

    async def send_audio(self, frame: bytes) -> None:
        pass

    async def receive(self) -> CascadeInbound:
        await asyncio.sleep(3600)
        raise AssertionError("이 테스트는 receive 를 쓰지 않는다")


def _rig() -> tuple[CascadeSession, list[str]]:
    """드레인만 보는 세션 — 실제 대답 대신 '무엇으로 몇 번 시작했나'를 받아 적는다."""
    session = CascadeSession(_Sink(), genai_client=object())
    started: list[str] = []

    async def _record(user_text: str, is_greeting: bool = False) -> None:
        started.append(user_text)

    session._start_reply = _record          # type: ignore[method-assign]
    return session, started


@pytest.mark.asyncio
async def test_two_queued_utterances_become_one_reply():
    """⭐ 2건 → **1회**. 그리고 두 말이 **둘 다** 살아 있어야 한다(앞말을 버리지 않는다)."""
    session, started = _rig()
    session._pending_user_texts = ["안녕하세요.", "여보세요?"]

    await session._drain_pending_user_text()

    assert len(started) == 1, f"밀린 발화마다 답했다 — 비버가 연달아 말한다: {started}"
    assert "안녕하세요." in started[0], "앞말을 버렸다(B 안이다 — 사장님은 A 를 고르셨다)"
    assert "여보세요?" in started[0], "뒷말이 사라졌다"
    assert started[0] == "안녕하세요. 여보세요?"


@pytest.mark.asyncio
async def test_three_queued_utterances_become_one_reply():
    """⭐ 3건이어도 **1회**. 개수와 무관하게 한 번이다."""
    session, started = _rig()
    session._pending_user_texts = ["안녕하세요.", "여보세요?", "들려요?"]

    await session._drain_pending_user_text()

    assert len(started) == 1, started
    assert started[0] == "안녕하세요. 여보세요? 들려요?"


@pytest.mark.asyncio
async def test_single_queued_utterance_is_unchanged():
    """⭐ 1건은 **예전과 완전히 같다** — 합치기가 흔한 경우를 바꾸면 안 된다."""
    session, started = _rig()
    session._pending_user_texts = ["지금 몇 시야"]

    await session._drain_pending_user_text()

    assert started == ["지금 몇 시야"], "1건인데 문자열이 가공됐다"


@pytest.mark.asyncio
async def test_draining_twice_never_answers_twice():
    """⛔ **중복 응답 0건** — 꺼내기는 원자적이다(리스트를 통째로 교체한다).

    남겨 두면 다음 드레인이 같은 말에 또 답한다. `_run_reply` 와 `_run_resume` 의 finally 가
    **둘 다** 이 함수를 지나므로, 한 통화에서 드레인이 연달아 불릴 수 있다.
    """
    session, started = _rig()
    session._pending_user_texts = ["안녕하세요.", "여보세요?"]

    await session._drain_pending_user_text()
    await session._drain_pending_user_text()
    await session._drain_pending_user_text()

    assert len(started) == 1, f"이미 답한 발화에 다시 답했다: {started}"
    assert session._pending_user_texts == [], "꺼내고도 대기열에 남겼다"


@pytest.mark.asyncio
async def test_empty_queue_starts_nothing():
    """빈 대기열은 아무것도 시작하지 않는다(공백만 있는 발화도 마찬가지)."""
    session, started = _rig()

    await session._drain_pending_user_text()
    session._pending_user_texts = ["   ", ""]
    await session._drain_pending_user_text()

    assert started == []


@pytest.mark.asyncio
async def test_queue_keeps_order_and_first_arrival_time():
    """⭐ 쌓이는 자리에서: **덮어쓰지 않고 append**, 대기 시각은 **첫 발화** 기준이다."""
    session, _started = _rig()
    session._tg = object()          # create_task 까지 갈 일이 없다(아래에서 앞 대답이 살아 있다)
    session.state = TurnState.BEAVER_SPEAKING
    session._audible_ms = lambda: 5_000      # 들리고 있으니 버리지 않는다 → 대기열로 간다
    session._turn_beaver_unheard = False     # 말을 시작한 때도 들리고 있었다

    async def _forever() -> None:
        await asyncio.sleep(10)

    running = asyncio.get_running_loop().create_task(_forever())
    session._reply_task = running
    session._start_reply = CascadeSession._start_reply.__get__(session)   # 진짜 경로로 되돌린다

    await session._start_reply("안녕하세요.")
    first_since = session._pending_since
    await asyncio.sleep(0.02)
    await session._start_reply("여보세요?")

    assert session._pending_user_texts == ["안녕하세요.", "여보세요?"], "덮어썼다(앞말 소실)"
    assert session._pending_since == first_since, (
        "대기 시각을 뒤엣것으로 갱신했다 — 기다린 시간이 짧게 나와 지연을 못 본다"
    )
    running.cancel()
