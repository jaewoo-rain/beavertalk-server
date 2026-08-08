"""캐스케이드 턴 경계 회귀 — **2026-08-06 실통화에서 처음 드러난 결함**들.

단위 테스트가 못 잡고 실통화에서 나왔다. 다시 놓치지 않도록 실측 수치를 그대로 박아 둔다.

관측된 것(로그 원문):
    u16 speech_ms=1463 lag=79461 text='안녕하세요'   16:09:49.499
    u17 speech_ms=0    lag=897   text='안녕하세요'   16:09:49.843   ← 0.34초 뒤 같은 말이 또 턴
    (u2/u3, u4/u5, u18/u19, u20/u21, u22/u23 도 같은 쌍)

원인: 침묵 타이머는 "이미 흘러간 침묵"을 오디오 시각으로 빼고 남은 만큼만 기다린다. 그 뺄셈은
**파이프라인 지연 < 침묵 임계**일 때만 성립하는데, 실측 지연이 810~914ms 로 임계(800ms)를
넘었다 → 남은 대기가 0 → speech_end 를 처리하는 순간 턴이 닫히고, 뒤늦게 온 최종 전사가
IDLE 에서 **턴을 하나 더 연다**. P1 에서는 비버가 같은 말에 두 번 대답하게 된다.

여기서 고정하는 성질:
  ① 지연이 임계를 넘어도 **한 발화는 한 턴**이다(최소 대기 바닥)
  ② 닫힌 턴의 꼬리 전사는 **새 턴을 열지 않는다**(유령 턴 차단)
  ③ 롤오버 재생은 **이미 최종 전사가 난 구간을 다시 흘리지 않는다**(재인식 = 턴 중복 + 이중 과금)
"""

import asyncio
import time

import pytest

import core.stt as stt_mod
from core.stt import SPEECH_BEGIN, SPEECH_END, TRANSCRIPT, RollingSttV2Stream, SttV2Event
from domains.learning.realtime.cascade_session import CascadeInbound, CascadeSession

# 실통화 실측치 — 이 숫자들이 결함의 조건이다(지연 > 임계).
LIVE_SILENCE_MS = 800
LIVE_LAG_MS = 900


class _Sink:
    """이벤트만 모으는 트랜스포트(펌프를 직접 돌리므로 receive 는 안 쓴다)."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.at: list[float] = []          # 이벤트가 나간 시각(닫히기까지 얼마 기다렸나 검사용)

    async def send_event(self, event: dict) -> None:
        self.events.append(event)
        self.at.append(time.monotonic())

    def time_of(self, type_: str) -> float:
        return next(t for e, t in zip(self.events, self.at) if e.get("type") == type_)

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def receive(self) -> CascadeInbound:
        await asyncio.sleep(3600)
        raise AssertionError("이 테스트는 receive 를 쓰지 않는다")

    def types(self) -> list[str]:
        return [e.get("type") for e in self.events]

    def all(self, type_: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == type_]


async def _drive(session: CascadeSession, script) -> None:
    """상태기계 펌프만 돌린다 — 이벤트를 오프셋과 함께 정확한 시점에 넣기 위해.

    실제 STT 를 통과시키면 오프셋을 마음대로 못 준다. 여기서 재현하려는 건 **오프셋과
    시각의 조합**이라 큐에 직접 넣는 게 유일하게 정확한 방법이다.
    """
    pump = asyncio.create_task(session._pump_turn())
    for item in script:
        if isinstance(item, float):
            await asyncio.sleep(item)
            continue
        await session._q.put(item)
    await asyncio.sleep(0.05)
    pump.cancel()
    with pytest.raises((asyncio.CancelledError, Exception)):
        await pump


def _session(monkeypatch, silence_ms=LIVE_SILENCE_MS) -> tuple[CascadeSession, _Sink]:
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_SILENCE_MS", silence_ms)
    sink = _Sink()
    session = CascadeSession(sink)
    session._silence_ms = silence_ms
    session._audio_ms = 10_000.0          # 지금까지 10초를 STT 로 흘렸다
    return session, sink


@pytest.mark.asyncio
async def test_one_utterance_stays_one_turn_when_lag_exceeds_threshold(monkeypatch):
    """⭐ 지연(900ms) > 침묵 임계(800ms) 여도 한 발화는 **한 턴**이다.

    예전 코드: 남은 대기 = 800 − 900 → 0 → speech_end 즉시 닫힘 → 뒤늦은 최종 전사가 두 번째
    턴을 열었다(실통화 u16/u17). 지금: 최소 대기 바닥이 최종 전사를 기다린다.
    """
    session, sink = _session(monkeypatch)
    audio = session._audio_ms
    await _drive(session, [
        SttV2Event(kind=SPEECH_BEGIN, offset_ms=int(audio - 2000)),
        SttV2Event(kind=TRANSCRIPT, text="안녕하세", is_final=False,
                   offset_ms=int(audio - 1000)),
        # speech_end 시점에 이미 900ms 가 흘렀다고 보고된다(= 실측 파이프라인 지연).
        SttV2Event(kind=SPEECH_END, offset_ms=int(audio - LIVE_LAG_MS)),
        0.05,
        # 최종 전사는 speech_end 보다 늦게 온다 — 실통화에서 0.02~0.9초 뒤였다.
        SttV2Event(kind=TRANSCRIPT, text="안녕하세요", is_final=True,
                   offset_ms=int(audio - LIVE_LAG_MS + 50)),
        0.4,
    ])
    ends = sink.all("user_turn_end")
    assert len(ends) == 1, sink.events          # ⛔ 2개면 P1 에서 비버가 두 번 답한다
    assert ends[0]["text"] == "안녕하세요"      # 최종 전사가 그 턴에 담겼다
    assert len(sink.all("user_turn_start")) == 1, sink.events


@pytest.mark.asyncio
async def test_tail_final_after_close_does_not_open_ghost_turn(monkeypatch):
    """닫힌 턴의 **꼬리 전사**는 새 턴을 열지 않는다(유령 턴 차단).

    바닥을 두더라도 그보다 더 늦게 오는 전사는 있다(롤오버 재인식 등). 그때도 같은 말이
    턴 2개가 되면 안 된다 — 오디오 시각이 앞 턴의 끝을 넘지 못하면 새 소리가 아니다.
    """
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_MIN_WAIT_MS", 30)
    session, sink = _session(monkeypatch, silence_ms=60)
    audio = session._audio_ms
    await _drive(session, [
        SttV2Event(kind=SPEECH_BEGIN, offset_ms=int(audio - 500)),
        SttV2Event(kind=TRANSCRIPT, text="가랑가랑", is_final=True, offset_ms=int(audio - 100)),
        SttV2Event(kind=SPEECH_END, offset_ms=int(audio - 100)),
        0.25,                                   # 턴이 닫힌다
        # 앞 턴이 이미 낸 발화의 꼬리(같은 오디오 지점) — 새 턴이면 안 된다.
        SttV2Event(kind=TRANSCRIPT, text="가랑가랑", is_final=True, offset_ms=int(audio - 120)),
        0.25,
    ])
    assert len(sink.all("user_turn_end")) == 1, sink.events
    assert len(sink.all("user_turn_start")) == 1, sink.events


@pytest.mark.asyncio
async def test_new_speech_after_close_still_opens_a_turn(monkeypatch):
    """⚠ 유령 턴 차단이 **진짜 다음 발화**를 삼키면 더 나쁘다 — 뒤로 간 오디오는 통과시킨다."""
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_MIN_WAIT_MS", 30)
    session, sink = _session(monkeypatch, silence_ms=60)
    audio = session._audio_ms
    await _drive(session, [
        SttV2Event(kind=TRANSCRIPT, text="첫마디", is_final=True, offset_ms=int(audio - 100)),
        SttV2Event(kind=SPEECH_END, offset_ms=int(audio - 100)),
        0.25,
        # 새 발화 — 오디오 시각이 앞 턴의 끝보다 확실히 뒤다.
        SttV2Event(kind=TRANSCRIPT, text="둘째마디", is_final=True, offset_ms=int(audio + 2000)),
        SttV2Event(kind=SPEECH_END, offset_ms=int(audio + 2000)),
        0.25,
    ])
    ends = sink.all("user_turn_end")
    assert [e["text"] for e in ends] == ["첫마디", "둘째마디"], sink.events


@pytest.mark.asyncio
async def test_absurd_offset_is_rejected_not_trusted(monkeypatch):
    """⭐ 결함 A — 상식 밖 오프셋은 **미상으로 거절**하고 전체 침묵을 기다린다.

    실측: `lag=79377ms kind=speech_begin offset_ms=860 audio_ms=73113` 인데 같은 통화의
    `stt_streams=1` — **롤오버 0회인데 79초**가 튀었다. 우리 기준점 문제가 아니라
    speech_event_offset 이 전역 오디오 타임라인이 아니라는 뜻이다.

    이 오염값을 믿으면 남은 대기가 0 이 되어 턴이 즉시 닫히고(결함 B 재발), barge-in 최소
    지속 게이트도 무조건 통과한다. 벤더의 의미를 가정하지 않고 **말이 안 되는 값을 버린다.**
    """
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_MIN_WAIT_MS", 20)
    session, sink = _session(monkeypatch, silence_ms=200)
    session._audio_ms = 73_113.0
    began = time.monotonic()
    await _drive(session, [
        SttV2Event(kind=SPEECH_BEGIN, offset_ms=860),      # 72초 과거를 가리킨다 = 거절
        SttV2Event(kind=TRANSCRIPT, text="안녕", is_final=True, offset_ms=860),
        SttV2Event(kind=SPEECH_END, offset_ms=860),
        0.4,
    ])
    ends = sink.all("user_turn_end")
    assert len(ends) == 1 and ends[0]["text"] == "안녕", sink.events
    # ⭐ 오염값을 믿었으면 remain=0 이라 speech_end 직후 닫혔을 것이다. 거절했으니 침묵
    #   임계(200ms)를 온전히 기다린다.
    assert sink.time_of("user_turn_end") - began >= 0.15, "오염된 오프셋을 믿고 일찍 닫았다"
    # 계측도 오염되지 않는다 — 79초짜리 lag 이 기록에 남으면 안 된다.
    assert ends[0]["pipeline_lag_ms"] < 3000, ends[0]


@pytest.mark.asyncio
async def test_plausible_offset_is_still_trusted(monkeypatch):
    """⚠ 거절이 지나치면 안 된다 — 실측 수준(0.9초)의 지연은 그대로 믿고 침묵에서 뺀다."""
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_MIN_WAIT_MS", 20)
    session, sink = _session(monkeypatch, silence_ms=1000)
    audio = session._audio_ms
    await _drive(session, [
        # ⚠ 순서가 요점이다: VAD 종료가 먼저 오고(전체 대기), 그 뒤 전사가 오면 그때 깎는다.
        #   실통화의 순서도 이쪽이다 — speech_end 가 전사보다 먼저 온다.
        SttV2Event(kind=SPEECH_END, offset_ms=int(audio - 900)),
        SttV2Event(kind=TRANSCRIPT, text="안녕", is_final=True, offset_ms=int(audio - 900)),
        0.25,   # 남은 대기 100ms → 이 안에 닫혀야 한다(1초 임계를 다 기다리면 안 된다)
    ])
    assert len(sink.all("user_turn_end")) == 1, sink.events


# --------------------------------------------------------------------------- #
# ③ 롤오버 재생 — 이미 확정된 구간을 다시 흘리지 않는다
# --------------------------------------------------------------------------- #
class _Child:
    def __init__(self) -> None:
        self.pushed = 0

    async def push_audio(self, pcm: bytes) -> None:
        self.pushed += len(pcm)

    def usage(self) -> dict:
        return {"billed_sum_ms": 0.0, "billed_max_ms": 0.0, "billed_msgs": 0}

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_rollover_replay_skips_already_finalized_audio():
    """⭐ 재생 버퍼에서 **이미 최종 전사가 난 구간**을 잘라낸다.

    안 자르면 새 스트림이 같은 발화를 다시 인식해 ① 턴이 중복되고 ② 그 구간이 이중 과금된다
    (원가 설계 §2-2 의 위험 — 실통화에서 중복 턴으로 드러났다).
    """
    rolling = RollingSttV2Stream(lambda: _Child(), 16000)
    await rolling.push_audio(b"\x00" * 32_000)       # 1초 — 버퍼로
    await rolling.push_audio(b"\x00" * 32_000)       # 총 2초
    rolling._last_final_ms = 1_500.0                 # 1.5초 지점까지 확정됐다
    child = _Child()
    rolling._cur = child
    await rolling._flush()

    assert child.pushed == 16_000                    # 남은 0.5초만 재생
    assert rolling._base_ms == 1_500                 # 기준점도 잘라낸 만큼 뒤로
    assert rolling.usage()["replay_audio_ms"] == pytest.approx(500.0)


@pytest.mark.asyncio
async def test_rollover_replay_keeps_unrecognized_audio():
    """확정된 게 없으면 **한 바이트도 버리지 않는다**(턴을 삼키면 안 된다)."""
    rolling = RollingSttV2Stream(lambda: _Child(), 16000)
    await rolling.push_audio(b"\x00" * 32_000)
    child = _Child()
    rolling._cur = child
    await rolling._flush()
    assert child.pushed == 32_000
    assert rolling._base_ms == 0


# --------------------------------------------------------------------------- #
# ⑤ 말했는데 빈 턴이 되고, 늦게 온 진짜 전사가 버려지던 것 (2026-08-07 사장님 통화)
#   "인사 안녕 이렇게 2글자를 인식 못 할 때가 있네"
#   u2 speech_ms=1042 text='' / u4 speech_ms=4271 text='' — 말은 했는데 전사가 비었다.
#   신호: 빈 턴은 lag 이 낮다(291·338·348) vs 전사 있는 턴(723~870) = **시계가 둘이다.**
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_vad_offset_does_not_shorten_the_wait(monkeypatch):
    """⭐ VAD 이벤트의 오프셋으로는 침묵을 깎지 않는다 — 기다리는 대상은 **전사**다.

    VAD 지연(300ms)만큼 깎고 닫으면, 전사 지연(800ms)으로 오는 진짜 말이 뒤늦게 도착해
    그 턴은 빈 채로 닫힌다. 두 시계를 같은 것으로 보면 안 된다.
    """
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_MIN_WAIT_MS", 20)
    session, sink = _session(monkeypatch, silence_ms=400)
    audio = session._audio_ms
    began = time.monotonic()
    await _drive(session, [
        SttV2Event(kind=SPEECH_BEGIN, offset_ms=int(audio - 2000)),
        # VAD 종료가 '방금 300ms 전'을 가리킨다 — 예전 코드면 100ms 만 기다리고 닫았다.
        SttV2Event(kind=SPEECH_END, offset_ms=int(audio - 300)),
        0.2,
        # 진짜 최종 전사는 800ms 지연으로 뒤늦게 온다.
        SttV2Event(kind=TRANSCRIPT, text="안녕", is_final=True, offset_ms=int(audio - 800)),
        0.5,
    ])
    ends = sink.all("user_turn_end")
    assert len(ends) == 1, sink.events
    assert ends[0]["text"] == "안녕", "말을 했는데 빈 턴으로 닫혔다"
    # VAD(300ms)만 보고 100ms 만에 닫았으면 전사(0.2초 뒤 도착)를 놓쳤다. 전사를 받고 나서
    # 닫혔다는 것 = VAD 오프셋으로 깎지 않았다는 뜻이다.
    assert sink.time_of("user_turn_end") - began >= 0.2


@pytest.mark.asyncio
async def test_late_final_is_kept_when_the_closed_turn_was_empty(monkeypatch):
    """⭐⭐ 빈 턴 뒤에 온 진짜 전사는 **버리지 않는다** — 전달한 게 없으니 중복일 수 없다.

    중복 차단(유령 턴)과 발화 보존은 둘 다 만족해야 한다. 한쪽만 고치면 다른 쪽이 되살아난다.
    """
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_MIN_WAIT_MS", 20)
    session, sink = _session(monkeypatch, silence_ms=60)
    audio = session._audio_ms
    await _drive(session, [
        SttV2Event(kind=SPEECH_BEGIN, offset_ms=int(audio - 500)),
        SttV2Event(kind=SPEECH_END, offset_ms=int(audio - 100)),
        0.25,                                    # 빈 턴으로 닫힌다
        # 그 발화의 최종 전사가 뒤늦게 온다. 오디오 지점은 닫힌 턴의 끝과 사실상 같다 —
        # 예전 규칙이면 '꼬리'로 분류돼 버려졌다.
        SttV2Event(kind=TRANSCRIPT, text="안녕", is_final=True, offset_ms=int(audio - 120)),
        SttV2Event(kind=SPEECH_END, offset_ms=int(audio - 120)),
        0.25,
    ])
    texts = [e["text"] for e in sink.all("user_turn_end")]
    assert "안녕" in texts, f"진짜 발화가 버려졌다: {texts}"


@pytest.mark.asyncio
async def test_tail_of_delivered_text_is_still_blocked(monkeypatch):
    """⚠ 반대쪽도 지킨다 — **이미 전달한** 말의 꼬리는 여전히 새 턴을 못 연다(유령 턴 차단)."""
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_MIN_WAIT_MS", 20)
    session, sink = _session(monkeypatch, silence_ms=60)
    audio = session._audio_ms
    await _drive(session, [
        SttV2Event(kind=TRANSCRIPT, text="가랑가랑", is_final=True, offset_ms=int(audio - 100)),
        SttV2Event(kind=SPEECH_END, offset_ms=int(audio - 100)),
        0.25,
        SttV2Event(kind=TRANSCRIPT, text="가랑가랑", is_final=True, offset_ms=int(audio - 120)),
        0.25,
    ])
    assert len(sink.all("user_turn_end")) == 1, sink.events


# --------------------------------------------------------------------------- #
# ⑥ 전사 기준 종료판정 (2026-08-08)
#   사장님: "차 안이면 잡음이 계속 들어가는데, STT 로 더 이상 글자가 안 나오면 0.8초 뒤
#   바로 넘기면 안 되나?" — 종료판정 축이 **음향(VAD)** 이라 차·카페에서는 VAD 가 영영
#   조용해지지 않고, 턴 상한까지 열려 있었다("안녕하세요" 한 마디에 수십 초).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_transcript_stop_closes_the_turn_even_while_vad_is_active(monkeypatch):
    """⭐ 글자가 나온 뒤에는 **VAD 가 활성이어도** 전사가 멎으면 닫는다(잡음 무시)."""
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_TRANSCRIPT_SILENCE_MS", 150)
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_MIN_WAIT_MS", 20)
    session, sink = _session(monkeypatch, silence_ms=5000)   # VAD 기준이면 5초 = 안 닫힌다
    audio = session._audio_ms
    await _drive(session, [
        SttV2Event(kind=SPEECH_BEGIN, offset_ms=int(audio - 500)),   # 이후 speech_end 는 안 온다
        SttV2Event(kind=TRANSCRIPT, text="안녕하세요", is_final=True, offset_ms=int(audio)),
        0.35,
    ])
    ends = sink.all("user_turn_end")
    assert len(ends) == 1, sink.events
    assert ends[0]["text"] == "안녕하세요"


@pytest.mark.asyncio
async def test_before_any_text_the_vad_rule_still_applies(monkeypatch):
    """⚠ 글자가 나오기 전에는 예전대로 VAD 기준이다 — 안 그러면 **말 시작 전에 닫힌다**.

    숨 고르는 동안엔 아직 글자가 없다. 그때 전사 기준을 적용하면 학습자가 입을 떼기도 전에
    턴이 닫힌다.
    """
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_TRANSCRIPT_SILENCE_MS", 100)
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_MIN_WAIT_MS", 20)
    session, sink = _session(monkeypatch, silence_ms=5000)
    audio = session._audio_ms
    await _drive(session, [
        SttV2Event(kind=SPEECH_BEGIN, offset_ms=int(audio - 200)),
        0.35,                                   # 전사 없이 시간만 흐른다(잡음/숨 고르기)
    ])
    assert sink.all("user_turn_end") == [], "글자도 없는데 턴을 닫았다"


@pytest.mark.asyncio
async def test_noise_speech_begin_does_not_cancel_the_transcript_countdown(monkeypatch):
    """⛔ 잡음이 speech_begin 을 계속 만들어도 카운트다운이 취소되면 안 된다.

    취소되면 위 규칙이 무력해져 차 안에서 영영 안 닫힌다. 그리고 유령 턴(speech_ms=0 중복)이
    생기지 않는 것도 같이 지킨다 — 87fdf7d 가 고친 그 결함이다.
    """
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_TRANSCRIPT_SILENCE_MS", 200)
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_MIN_WAIT_MS", 20)
    session, sink = _session(monkeypatch, silence_ms=5000)
    audio = session._audio_ms
    await _drive(session, [
        SttV2Event(kind=SPEECH_BEGIN, offset_ms=int(audio - 500)),
        SttV2Event(kind=TRANSCRIPT, text="안녕하세요", is_final=True, offset_ms=int(audio)),
        0.05,
        SttV2Event(kind=SPEECH_BEGIN, offset_ms=int(audio + 10)),    # 잡음
        SttV2Event(kind=SPEECH_BEGIN, offset_ms=int(audio + 20)),    # 잡음
        0.35,
    ])
    ends = sink.all("user_turn_end")
    assert len(ends) == 1, sink.events
    assert ends[0]["text"] == "안녕하세요", ends
    # 유령 턴이 없다 = 같은 발화가 턴 2개가 되지 않았다(87fdf7d 가 고친 그 결함).
    # ⚠ speech_ms 는 이 하네스에서 이벤트를 한꺼번에 만들어 0 이 나온다 — 그건 하네스
    #   산물이라 판정에 쓰지 않고, **턴 개수**로 본다.
    assert len(sink.all("user_turn_start")) == 1, sink.events


def test_unfinished_sentence_is_observed_not_enforced():
    """⛔ 미완 판정은 **로그 전용**이다 — 판정에 쓰면 규칙 기반 오판이 대화를 끊는다."""
    from domains.learning.realtime.cascade_session import _looks_unfinished

    assert _looks_unfinished("제가") is True          # 조사로 끝났다
    assert _looks_unfinished("어제 학교에 갔는데") is True
    assert _looks_unfinished("안녕하세요.") is False   # 종결부호
    assert _looks_unfinished("안녕하세요") is False    # 종결어미(조사·연결어미 아님)
    assert _looks_unfinished("") is False
