"""상태기계 회귀 — **상태 조합**으로 세운다(2026-08-11 독립 QA 발견1·2).

QA 원문: "발견1·2 는 **상태 조합을 세우는 테스트가 있으면 잡혔을 종류**다."
회귀 800건이 그걸 못 잡았다는 게 이번 사고의 교훈이라, 여기서는 **상태 × 턴** 격자를 직접 세운다.

## 발견1 — 비버 발화 중 열린 턴에는 침묵 타이머가 안 걸렸다
`_open_turn` 은 비버가 말하는 중이면 **일부러 상태를 안 뺏는다**(barge-in 겹침 허용).
그런데 `_on_speech_end` 는 `state == USER_SPEAKING` 을 요구했다 → 조기 반환 →
`_close_at` 이 한 번도 안 걸린다. ⛔ barge-in 이 켜져 있으면 그게 **주 경로**다.
글자가 끝내 안 나온 턴은 30초 상한까지 갔다(사장님이 겪으신 침묵과 같은 모양).

## 발견2 — `CANCELLING` 을 푸는 전이가 **0개**였다
굳으면 `_open_turn` 이 그걸 보존 목록에 두므로 이후 모든 턴이 USER_SPEAKING 이 못 되고
**발견1이 영구화**된다(한 번 굳으면 통화 끝까지 침묵).

여기서 고정하는 성질:
  ① 턴이 열려 있으면 **비버 상태와 무관하게** speech_end 가 침묵 타이머를 건다
  ② 취소가 끝나면 CANCELLING 이 **반드시** 풀린다(두 층: 대답 finally · 턴 닫힘)
  ③ 아직 도는 대답이 있으면 **안 푼다**(취소 배관이 자기 상태를 잃으면 안 된다)
  ④ 늦게 죽은 옛 대답이 **새 대답의 상태를 덮지 않는다**
"""

import asyncio

import pytest

from core.stt import SPEECH_END, TRANSCRIPT, SttV2Event
from domains.learning.realtime.cascade_session import CascadeSession, TurnState


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send_event(self, event: dict) -> None:
        self.events.append(event)

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def receive(self):
        raise AssertionError("쓰지 않는다")


def _session() -> CascadeSession:
    session = CascadeSession(_Sink())
    session._audio_ms = 10_000.0
    return session


# ── ① 턴 축과 상태 축을 가른다 ─────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("state", [
    TurnState.USER_SPEAKING,
    TurnState.BEAVER_SPEAKING,   # ⭐ barge-in 이 켜지면 **주 경로**다
    TurnState.THINKING,
    TurnState.CANCELLING,        # ⛔ 굳었던 상태 — 여기서도 턴은 닫혀야 한다
])
async def test_speech_end_arms_the_silence_timer_in_every_beaver_state(state):
    """⭐ `_on_speech_end` 가 묻는 것은 "누가 말하는가"가 아니라 **"닫을 턴이 있는가"** 다."""
    session = _session()
    await session._open_turn(0.0)
    session.state = state
    assert session._close_at is None
    await session._on_speech_end(
        SttV2Event(kind=SPEECH_END, offset_ms=int(session._audio_ms - 100))
    )
    assert session._close_at is not None, f"{state} 에서 침묵 타이머가 안 걸렸다"


@pytest.mark.asyncio
async def test_speech_end_without_an_open_turn_is_ignored():
    """열린 턴이 없으면 무시한다(스트림 시작 직후의 잔여 이벤트)."""
    session = _session()
    session.state = TurnState.BEAVER_SPEAKING
    await session._on_speech_end(SttV2Event(kind=SPEECH_END, offset_ms=0))
    assert session._close_at is None


@pytest.mark.asyncio
async def test_open_turn_still_preserves_the_beaver_state():
    """⛔ 원래 의도를 깨지 않는다 — 비버가 말하는 중이면 상태를 뺏지 않는다.

    (뺏으면 취소·종료 경로가 자기 상태를 잃는다. 우리가 고친 건 **그 상태를 요구하던 쪽**이다.)
    """
    for state in (TurnState.BEAVER_SPEAKING, TurnState.THINKING, TurnState.CANCELLING):
        session = _session()
        session.state = state
        await session._open_turn(0.0)
        assert session.state is state
    idle = _session()
    await idle._open_turn(0.0)
    assert idle.state is TurnState.USER_SPEAKING


# ── ② CANCELLING 은 반드시 풀린다 ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_cancelling_is_settled_when_the_turn_closes(caplog):
    """⛔ barge-in 뒤 **빈 턴**으로 닫히는 경로 — 예전엔 여기서 CANCELLING 이 굳었다.

    (빈 텍스트면 `_start_reply` 가 안 불리고 `_resume_interrupted` 도 유예를 넘기면 False 라,
     상태를 덮어 줄 사람이 아무도 없었다.)
    """
    caplog.set_level("INFO")
    session = _session()
    await session._open_turn(0.0)
    session.state = TurnState.CANCELLING
    await session._close_turn("silence")           # 빈 턴(글자 없음)
    assert session.state is TurnState.IDLE, "CANCELLING 이 굳었다 — 이후 모든 턴이 죽는다"
    assert any("CANCELLING" in m for m in caplog.messages)


@pytest.mark.asyncio
async def test_cancelling_survives_while_a_reply_is_still_running():
    """③ 아직 도는 대답이 있으면 **안 푼다** — 취소 배관이 자기 상태를 잃으면 안 된다."""
    session = _session()

    async def _forever() -> None:
        await asyncio.sleep(10)

    session._reply_task = asyncio.get_running_loop().create_task(_forever())
    session.state = TurnState.CANCELLING
    session._settle_cancelling()
    assert session.state is TurnState.CANCELLING
    session._reply_task.cancel()


@pytest.mark.asyncio
async def test_cancelling_survives_while_the_beaver_turn_is_open():
    """비버 턴이 아직 열려 있으면 취소가 안 끝난 것이다."""
    session = _session()
    await session.beaver.begin()
    session.state = TurnState.CANCELLING
    session._settle_cancelling()
    assert session.state is TurnState.CANCELLING


@pytest.mark.asyncio
async def test_a_stale_reply_does_not_overwrite_the_new_one():
    """④ 늦게 죽은 옛 대답이 **새 대답의 THINKING 을 IDLE 로 덮으면** 그것도 굳음이다.

    세대 번호가 맞을 때만 되돌린다.
    """
    session = _session()
    session._reply_seq = 7                     # 지금 도는 대답은 7세대
    session.state = TurnState.THINKING
    session._settle_reply_state(5)             # 5세대(옛 것)가 뒤늦게 끝났다
    assert session.state is TurnState.THINKING, "옛 대답이 새 대답의 상태를 덮었다"
    session._settle_reply_state(7)             # 자기 세대면 되돌린다
    assert session.state is TurnState.IDLE


# ── ①+② 물린 지점: 굳은 뒤에도 다음 턴이 정상으로 닫힌다 ──────────────────
@pytest.mark.asyncio
async def test_a_turn_after_a_barge_in_still_closes_by_silence():
    """⭐ 두 발견이 물리는 자리 — 굳은 CANCELLING 이 이후 턴의 침묵 타이머를 죽였다.

    지금은 ①턴 축으로 판정하고 ②취소가 끝나면 풀리므로, 그다음 턴은 정상이다.
    """
    session = _session()
    session.state = TurnState.CANCELLING           # barge-in 직후
    await session._open_turn(0.0)                  # 보존 목록이라 상태는 그대로
    await session._on_speech_end(
        SttV2Event(kind=SPEECH_END, offset_ms=int(session._audio_ms - 100))
    )
    assert session._close_at is not None            # ① 침묵 축이 산다
    await session._close_turn("silence")
    assert session.state is TurnState.IDLE          # ② 취소가 풀린다
    await session._open_turn(1.0)
    assert session.state is TurnState.USER_SPEAKING, "다음 턴이 여전히 굳어 있다"


# ── B. 펌프를 멈추지 않는다 ────────────────────────────────────────────────
def test_bargein_gate_never_sleeps_in_the_pump():
    """⛔ `_bargein_allowed` 는 `_pump_turn` 본체에서 돈다 — 여기서 자면 **네 시계가 멈춘다**.

    오프셋이 -1 이면 옛 조건이 **항상 참**이라 `speech_begin` 마다 200ms 를 잤고, 지금 기본
    STT(openai)는 전사에 오프셋을 안 싣는다 = 기본 구성에서 상시 발생이었다(QA 발견5).
    """
    import ast
    import inspect
    import textwrap

    # ⚠ 문자열 검색으로 하면 **주석에 적힌 설명**까지 걸린다(실제로 걸렸다). 호출 노드로 본다.
    tree = ast.parse(textwrap.dedent(inspect.getsource(CascadeSession._bargein_allowed)))
    sleeps = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == "sleep"
    ]
    assert not sleeps, "펌프 본체에서 자면 네 시계가 전부 멈춘다"


# ══════════════════════════════════════════════════════════════════════════
# 대답 **경로** 축 — QA R1(2026-08-11)
#   지적: "격자가 상태로만 짜였고 **대답 경로(reply / resume / 배치 / 가짜비버) 축이 없다.**
#         R1 세 결함은 전부 '다른 경로'라 통과했다."
#   실제로 `_run_resume` 에는 세 가지가 통째로 빠져 있었다:
#     ①대기열 배수(=사용자 말이 사라진다) ②CANCELLING 해제 ③세대 가드
# ══════════════════════════════════════════════════════════════════════════
def _reply_paths():
    """대답을 내는 **모든 경로**의 (이름, 코루틴 소스). 새 경로가 생기면 여기 추가한다."""
    import inspect

    return [
        ("reply", inspect.getsource(CascadeSession._run_reply)),
        ("resume", inspect.getsource(CascadeSession._run_resume)),
    ]


@pytest.mark.parametrize("name,src", _reply_paths())
def test_every_reply_path_drains_the_queue(name, src):
    """⛔ **모든 대답 경로가 대기열을 배수해야 한다.**

    안 하면 그 경로가 도는 동안 한 말이 **영영 답을 못 받는다.** 그리고 사용자가 참다 다시
    말하면 새 대답의 finally 가 낡은 발화를 배수해 **침묵 → 새 말 답 → 낡은 말 답** 순으로 들린다.
    """
    assert "_drain_pending_user_text" in src, f"{name} 경로가 대기열을 안 비운다"


@pytest.mark.parametrize("name,src", _reply_paths())
def test_every_reply_path_settles_state_with_a_generation_guard(name, src):
    """⛔ 상태 되돌리기는 **한 곳**(`_settle_reply_state`)을 거친다.

    직접 `state = IDLE` 로 되돌리면 ①CANCELLING 이 안 풀리고 ②늦게 죽은 옛 태스크가
    **새 대답의 THINKING 을 덮는다**. 두 결함 다 실제로 있었다.
    """
    assert "_settle_reply_state" in src, f"{name} 경로가 상태를 직접 되돌린다"


@pytest.mark.asyncio
async def test_resume_takes_a_generation_number():
    """세대 가드가 실제로 걸리는지 — 옛 resume 이 새 대답 상태를 못 덮는다."""
    import inspect

    assert "seq" in inspect.signature(CascadeSession._run_resume).parameters
    session = _session()
    session._reply_seq = 3
    session.state = TurnState.THINKING
    session._settle_reply_state(2)            # 옛 resume 이 뒤늦게 끝났다
    assert session.state is TurnState.THINKING


def test_batch_reply_is_inline_so_it_inherits_the_reply_finally():
    """⭐ 배치(Gemini)는 **별도 태스크가 아니다** — `_run_reply` 안에서 await 된다.

    그래서 대기열 배수·상태 정리를 그대로 물려받는다. (별도 태스크가 되는 순간 위 두 성질을
    직접 갖춰야 한다 — 이 테스트가 그 변화를 알려 준다.)
    """
    import inspect

    assert "await self._run_batch_reply(" in inspect.getsource(CascadeSession._run_reply)


@pytest.mark.asyncio
async def test_fake_beaver_never_clears_a_real_reply_state():
    """⚠ dev 훅과 대답 경로는 **다른 태스크 축**이다 — 훅의 finally 가 진짜 대답 상태를
    덮으면 말하는 중인 비버의 상태가 사라진다."""
    import inspect

    src = inspect.getsource(CascadeSession._run_fake_beaver)
    assert "_reply_task is None or self._reply_task.done()" in src, src[-500:]
