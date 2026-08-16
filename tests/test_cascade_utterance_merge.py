"""⛔⛔ **벤더 VAD 가 한 발화를 쪼개면 비버가 대답을 만들다 버리고 다시 만든다**(00148).

실통화 원문:
    u1 speech_ms=2294 미완=yes '스터디 코리안 투데…'
    u2 speech_ms=230              '영 한국어 공부하자.'   ← 1초 뒤
    → **준비 중이던 대답을 버린다** → 새 발화에 답한다 (대답 취소, b2 못들려준=75)
사장님 체감: "응답이 느리다". 실제로 **두 번 만든다.**

⭐ 원인은 중복 확정이 **아니다**(내가 실제 벤더에 붙여 이벤트 전량을 받아 확인했다):
    item1  audio 52~1728ms   'Study Korean Today.'
    item2  audio 1812~3808ms '영 한국어 공부하자.'      ← **오디오 간격 84ms**
서로 다른 구간이다 — 벤더 server_vad 가 문장 사이 짧은 숨에서 **한 발화를 둘로 나눈다**.
`speech_ms=230` 은 두 번째 `speech_started` 가 벽시계로 늦게(453ms) 도착해 우리 시계에
짧게 잡힌 값이다.

여기서 고정하는 성질:
  ① 오디오 간격이 좁으면(≤ merge gap) **한 발화로 잇는다** — 턴을 닫지 않는다
  ② 이미 닫혔으면 앞 조각 텍스트를 **새 턴 앞에 붙인다**(뒤 절반에만 답하면 안 된다)
  ③ ⛔ **진짜 두 번째 발화는 삼키지 않는다** — 사람이 두는 쉼(≥500ms)은 새 턴이다
  ④ 오프셋을 모르는 엔진에서는 **잇지 않는다**(모르는 값으로 합치면 발화를 삼킨다)
"""

import time

import pytest

import domains.learning.realtime.cascade_session as cs
from core.stt import SPEECH_BEGIN, SPEECH_END, TRANSCRIPT, SttV2Event


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send_event(self, event: dict) -> None:
        self.events.append(event)

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def receive(self):
        raise AssertionError("쓰지 않는다")


def _session() -> cs.CascadeSession:
    session = cs.CascadeSession(_Sink())
    session._silence_ms = cs.settings.CASCADE_TURN_SILENCE_MS
    return session


def _ev(kind: str, offset_ms: int, text: str = "", final: bool = False) -> SttV2Event:
    return SttV2Event(kind=kind, offset_ms=offset_ms, text=text, is_final=final,
                      at=time.monotonic())


async def _speak(session, begin_ms, end_ms, text):
    """한 조각: speech_started → 최종 전사 → speech_stopped."""
    await session._on_speech_begin(_ev(SPEECH_BEGIN, begin_ms))
    await session._on_transcript(_ev(TRANSCRIPT, end_ms, text, final=True))
    await session._on_speech_end(_ev(SPEECH_END, end_ms))


# ── ① 열린 턴을 안 닫는다 ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_vad_split_keeps_the_turn_open():
    """실측 그대로: 52~1728ms 뒤 1812ms 에 다시 시작 = **간격 84ms**."""
    session = _session()
    await _speak(session, 52, 1728, "Study Korean Today.")
    assert session._close_at is not None, "닫기 카운트다운이 걸려 있어야 한다(전제)"

    await session._on_speech_begin(_ev(SPEECH_BEGIN, 1812))
    assert session._close_at is None, "쪼개진 뒤 조각인데 턴이 닫히려 한다"
    assert session._turn_id == "u1", "새 턴이 열렸다 — 같은 발화가 둘로 갈렸다"


@pytest.mark.asyncio
async def test_the_whole_utterance_becomes_one_turn():
    """두 조각이 **한 턴의 한 문장**으로 닫힌다."""
    session = _session()
    await _speak(session, 52, 1728, "Study Korean Today.")
    await _speak(session, 1812, 3808, "영 한국어 공부하자.")
    await session._close_turn()
    ends = [e for e in session.transport.events if e.get("type") == "user_turn_end"]
    assert len(ends) == 1, ends
    assert ends[0]["text"] == "Study Korean Today. 영 한국어 공부하자."


# ── ③ 진짜 두 번째 발화는 새 턴 ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_real_second_utterance_still_opens_a_new_turn():
    """⛔ 사람이 일부러 두는 쉼(여기선 1.2초)은 **새 발화**다 — 삼키면 안 된다."""
    session = _session()
    await _speak(session, 52, 1728, "안녕하세요.")
    await session._close_turn()
    await session._on_speech_begin(_ev(SPEECH_BEGIN, 1728 + 1200))
    assert session._turn_id == "u2", "두 번째 발화가 새 턴이 안 됐다"
    assert session._turn_carry == "", "남의 말이 앞에 붙었다"


@pytest.mark.asyncio
async def test_the_merge_gap_sits_between_the_split_and_a_human_pause():
    """값의 성질: 실측 갈림은 잇고, 사람이 일부러 두는 쉼은 안 잇는다.

    ⚠ **2026-08-14 에 단위를 고쳤다.** 예전 회귀는 `gap < 임계/2` 를 못박았다:

        assert 84 < gap < 500
        assert gap < CASCADE_TURN_SILENCE_MS / 2

    ⛔ 그런데 이 `gap` 은 **벤더가 침묵창을 이미 뺀 뒤의 값**이다. 사람이 실제로 쉴 수 있는
      시간은 `gap + 침묵창` 이고, 침묵창은 그때 우리가 **모르던 값**(벤더 기본값)이었다.
      단위가 틀린 채로 못박혀 있었던 것이다 — 침묵창을 500→300 으로 내리자 같은 `gap` 이
      전혀 다른 관용도를 뜻하게 되면서 드러났다.
    ⭐ 그 규칙이 지키려던 **성질은 그대로 살린다**: "진짜 두 번째 발화를 삼키지 않는다".
      다만 올바른 단위로 다시 쓴다 — **허용 쉼(gap + 침묵창)이 턴을 닫는 임계를 넘지 않는다.**
      넘으면 "우리가 턴을 닫을 만큼 긴 쉼"인데도 같은 발화로 붙여 버려 종료 판정이 무의미해진다.
    """
    gap = cs.settings.CASCADE_SPEECH_MERGE_GAP_MS
    vendor_silence = cs.settings.OPENAI_STT_SILENCE_MS
    tolerated_pause = gap + vendor_silence

    # ① 실측 갈림은 잇는다. 벤더가 한 문장을 둘로 쪼갠 그 사건은 침묵창 500 기준 84ms 였고,
    #    침묵창이 S 면 같은 사건의 간격은 `84 + (500 − S)` 로 나타난다.
    measured_split = 84 + (500 - vendor_silence)
    assert measured_split < gap, (
        f"실측 갈림({measured_split}ms)이 병합창({gap}ms) 밖이다 — 한 문장이 둘로 갈린다"
    )
    assert measured_split <= gap * 0.7, (
        f"실측 갈림이 병합창의 {measured_split / gap:.0%} 다 — 여유가 없어 조금만 흔들려도 갈린다"
    )

    # ② ⛔ 그래도 **턴을 닫을 만큼 긴 쉼**까지 붙이지는 않는다(예전 규칙이 지키던 성질).
    assert tolerated_pause <= cs.settings.CASCADE_TURN_SILENCE_MS, (
        f"허용 쉼 {tolerated_pause}ms 가 턴 임계 {cs.settings.CASCADE_TURN_SILENCE_MS}ms 를 "
        "넘는다 — 턴 종료 판정이 무의미해진다"
    )


@pytest.mark.asyncio
async def test_widening_the_merge_window_cannot_make_the_close_timer_earlier():
    """⭐⭐ **병합창을 넓히는 것은 단조 안전하다** — 이미 병합되던 발화를 놓치게 만들 수 없다.

    2026-08-14 에 "gap=500 이면 벽시계 여유가 500→300ms 로 준다"는 걱정이 나왔다. 그 산수는
    **최악으로 허용되는 gap** 을 기준으로 한 것이고, 실제 경합은 그 발화의 **실제 gap** 으로
    일어난다. 그리고 닫기 시계는 `CASCADE_SPEECH_MERGE_GAP_MS` 를 **읽지 않는다** —
    창을 넓혀도 마감은 1ms 도 안 당겨진다. 넓히면 **오늘은 아예 병합 대상이 아니던** 발화가
    후보로 들어올 뿐이고, 그것들은 놓쳐도 `_carry_over_text` 그물이 받는다.

    ⛔ 이 성질이 깨지면(닫기 시계가 병합창을 참조하게 되면) 위 걱정이 **진짜가 된다.**
    """
    session = _session()
    session._audio_ms = 10_000.0
    session._turn_id = "u1"
    event = SttV2Event(kind=SPEECH_END, offset_ms=int(session._audio_ms - 172))

    cs.settings.CASCADE_SPEECH_MERGE_GAP_MS  # noqa: B018 - 아래 monkeypatch 대상 명시
    deadlines = []
    for gap in (0, 300, 500, 900):
        cs.settings.CASCADE_SPEECH_MERGE_GAP_MS = gap
        session._arm_close_timer(event)
        deadlines.append(round(session._close_at - time.monotonic(), 2))
    cs.settings.CASCADE_SPEECH_MERGE_GAP_MS = 500

    assert len(set(deadlines)) == 1, f"병합창이 닫기 시계를 움직였다 — {deadlines}"


@pytest.mark.asyncio
async def test_lowering_the_vendor_silence_alone_would_shrink_the_tolerance():
    """⛔ **둘은 짝이다.** 침묵창만 내리면 초보 학습자의 쉼이 그만큼 잘린다.

    이 성질을 회귀로 두는 이유: 다음 사람이 지연을 줄이려고 `OPENAI_STT_SILENCE_MS` 만
    또 내릴 것이기 때문이다. 그때 `CASCADE_SPEECH_MERGE_GAP_MS` 를 같이 안 올리면
    **말이 문장 중간에서 잘린다** — 우리 사용자층에서 제일 비싼 실패다.
    """
    gap = cs.settings.CASCADE_SPEECH_MERGE_GAP_MS
    vendor_silence = cs.settings.OPENAI_STT_SILENCE_MS
    # 이 값들이 어떻게 바뀌든, 둘의 합은 예전 관용도(800ms) 아래로 **내려가면 안 된다**.
    assert gap + vendor_silence >= 800, (
        f"허용 쉼이 {gap + vendor_silence}ms 로 후퇴했다 — 한쪽만 건드렸다"
    )


# ── ② 닫힌 뒤에 오면 앞부분을 넘긴다 ───────────────────────────────────────
@pytest.mark.asyncio
async def test_the_first_half_is_carried_into_the_new_turn():
    """⛔ 안 넘기면 비버가 **문장 뒤 절반에만** 답한다."""
    session = _session()
    await _speak(session, 52, 1728, "Study Korean Today.")
    await session._close_turn()                       # 타이머가 먼저 닫아 버린 경우
    await session._on_speech_begin(_ev(SPEECH_BEGIN, 1812))
    assert session._turn_carry == "Study Korean Today."

    await session._on_transcript(_ev(TRANSCRIPT, 3808, "영 한국어 공부하자.", final=True))
    await session._on_speech_end(_ev(SPEECH_END, 3808))
    await session._close_turn()
    ends = [e for e in session.transport.events if e.get("type") == "user_turn_end"]
    assert ends[-1]["text"] == "Study Korean Today. 영 한국어 공부하자.", ends[-1]["text"]


@pytest.mark.asyncio
async def test_nothing_is_carried_after_the_grace_window(monkeypatch):
    """⚠ 유예 창을 넘겨 도착하면 **잇지 않는다** — 옛말이 새 발화에 붙으면 안 된다."""
    session = _session()
    await _speak(session, 52, 1728, "Study Korean Today.")
    await session._close_turn()
    session._closed_at -= max(1.0, cs.settings.CASCADE_STALE_FINAL_MS / 1000.0 + 1.0)
    await session._on_speech_begin(_ev(SPEECH_BEGIN, 1812))
    assert session._turn_carry == ""


# ── ④ 오프셋이 없으면 잇지 않는다 ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_without_offsets_we_never_merge():
    """⛔ 모르는 값으로 발화를 합치면 **진짜 두 번째 발화를 삼킨다**(그게 더 나쁘다)."""
    session = _session()
    await _speak(session, -1, -1, "안녕하세요.")
    await session._close_turn()
    await session._on_speech_begin(_ev(SPEECH_BEGIN, -1))
    assert session._turn_carry == ""
    assert session._turn_id == "u2"


def test_merging_can_be_turned_off(monkeypatch):
    """0 이면 예전 동작(안 잇는다) — 되돌릴 길을 남긴다."""
    session = _session()
    monkeypatch.setattr(cs.settings, "CASCADE_SPEECH_MERGE_GAP_MS", 0)
    assert session._is_same_utterance(_ev(SPEECH_BEGIN, 1812), 1728) is False
