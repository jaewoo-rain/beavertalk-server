"""캐스케이드 턴 상태기계 — 페이크 STT v2 로 WS 계약 검증(크레덴셜·과금 0).

여기서 확인하는 것:
  ① speech_begin → transcript → speech_end 가 user_turn_start/input_transcript/user_turn_end 로 나가나
  ② VAD BEGIN 없이 전사가 먼저 와도 턴이 열리나(엔진 방어)
  ③ **턴 종료는 서버 자체 타이머**다 — speech_end 즉시가 아니라 침묵 임계 뒤에 닫힌다
     (STT 의 voice_activity_timeout 은 스트림을 닫는 필드라 턴 노브로 못 쓴다)
  ④ 발화가 재개되면(speech_begin 재발생) 카운트다운이 취소되고 같은 턴이 이어진다
  ⑤ 오디오 오프셋이 있으면 **이미 흘러간 침묵을 빼고** 남은 만큼만 기다린다
  ⑥ 롤오버 이벤트가 stt_rollover 로 중계되나 — **턴을 끊지 않고**
  ⑦ 이름 규율: 사용자 턴은 user_turn_* 다(비버 턴 turn_end 와 겹치면 앱 재생이 깨진다)
"""

import asyncio

import pytest

import core.stt as stt_mod
from core.stt import (
    SPEECH_BEGIN,
    SPEECH_END,
    STREAM_ROLLOVER,
    TRANSCRIPT,
    SttV2Event,
)
from domains.learning.realtime.cascade_session import CascadeInbound, CascadeSession


@pytest.fixture
def fake_v2(monkeypatch):
    """STT_V2_FAKE 를 켜고 클라 캐시를 비워 make_stt_v2_stream 이 페이크로 폴백하게 한다.

    침묵 임계는 테스트에서 짧게(60ms) — 판정 로직은 같고 대기 시간만 줄인다.
    """
    monkeypatch.setattr(stt_mod.settings, "STT_V2_FAKE", True)
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_SILENCE_MS", 60)
    stt_mod.get_speech_v2_client.cache_clear()
    yield
    stt_mod.get_speech_v2_client.cache_clear()


class _StubTransport:
    """스크립트된 inbound 를 순서대로 내주고, 나간 이벤트를 수집한다.

    stt_session 테스트와 같은 규율: 기다리는 이벤트가 나갈 때까지 stop 을 미뤄 레이스 제거.
    """

    def __init__(self, scripted, wait_for="user_turn_end") -> None:
        self._scripted = list(scripted)
        self.events: list[dict] = []
        self._wait_for = wait_for
        self._done = asyncio.Event()

    async def send_event(self, event: dict) -> None:
        self.events.append(event)
        if event.get("type") == self._wait_for:
            self._done.set()

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def receive(self) -> CascadeInbound:
        if self._scripted:
            item = self._scripted.pop(0)
            if isinstance(item, float):  # 스크립트 사이의 지연
                await asyncio.sleep(item)
                return await self.receive()
            return item
        await self._done.wait()
        return CascadeInbound(kind="control", control={"type": "stop"})

    def types(self) -> list[str]:
        return [e.get("type") for e in self.events]

    def first(self, type_: str) -> dict | None:
        return next((e for e in self.events if e.get("type") == type_), None)


def _ctl(**kwargs) -> CascadeInbound:
    return CascadeInbound(kind="control", control=kwargs)


@pytest.mark.asyncio
async def test_full_turn_begin_transcript_end(fake_v2):
    """정상 경로: 말 시작 → 전사 → 말 끝 = 턴 1건이 계측과 함께 나간다."""
    transport = _StubTransport(
        [
            _ctl(type="start", sampleRate=16000),
            _ctl(type="__test_event", event=SPEECH_BEGIN),
            _ctl(type="__test_say", text="안녕하세요"),
            0.05,
            _ctl(type="__test_event", event=SPEECH_END),
        ]
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)

    types = transport.types()
    assert types[0] == "ready", transport.events
    assert "user_turn_start" in types, transport.events
    assert "input_transcript" in types, transport.events

    end = transport.first("user_turn_end")
    assert end is not None, transport.events
    assert end["text"] == "안녕하세요"
    assert end["turn_id"] == transport.first("user_turn_start")["turn_id"]
    assert end["speech_ms"] >= 0
    assert end["reason"] == "silence"        # 서버 타이머가 닫았다
    assert end["silence_ms"] == 60

    # ⛔ 이름 규율: 비버 턴(turn_end)과 겹치면 앱의 재생 상태기계가 깨진다(클라 제약 #1).
    assert "turn_end" not in types
    assert "turn_start" not in types


@pytest.mark.asyncio
async def test_ready_reports_engine_and_threshold(fake_v2):
    """ready 는 엔진 종류와 침묵 임계를 알린다 — 데모가 '페이크로 도는 중'을 숨기지 않게."""
    transport = _StubTransport([_ctl(type="start")], wait_for="ready")
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    ready = transport.first("ready")
    assert ready["engine"] == "fake"
    assert ready["turn_silence_ms"] == stt_mod.settings.CASCADE_TURN_SILENCE_MS
    assert ready["bargein_min_ms"] == stt_mod.settings.CASCADE_BARGEIN_MIN_MS


@pytest.mark.asyncio
async def test_transcript_without_speech_begin_opens_turn(fake_v2):
    """VAD BEGIN 없이 전사가 먼저 오는 엔진/설정에서도 턴이 열려야 한다(방어)."""
    transport = _StubTransport(
        [
            _ctl(type="start"),
            _ctl(type="__test_say", text="바다"),
            0.05,
            _ctl(type="__test_event", event=SPEECH_END),
        ]
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    assert "user_turn_start" in transport.types(), transport.events
    assert transport.first("user_turn_end")["text"] == "바다"


@pytest.mark.asyncio
async def test_speech_end_before_final_waits_briefly(fake_v2, monkeypatch):
    """speech_end 가 최종 전사보다 먼저 와도, 침묵 창 안에 온 전사를 담아 닫는다."""
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_SILENCE_MS", 400)
    transport = _StubTransport(
        [
            _ctl(type="start"),
            _ctl(type="__test_event", event=SPEECH_BEGIN),
            _ctl(type="__test_event", event=SPEECH_END),
            0.05,
            _ctl(type="__test_say", text="늦게 온 전사"),
        ]
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    end = transport.first("user_turn_end")
    assert end is not None and end["text"] == "늦게 온 전사", transport.events


@pytest.mark.asyncio
async def test_speech_end_without_transcript_closes_after_grace(fake_v2):
    """침묵 임계가 지나면 전사가 없어도 턴을 닫는다(무한 대기 금지)."""
    transport = _StubTransport(
        [
            _ctl(type="start"),
            _ctl(type="__test_event", event=SPEECH_BEGIN),
            _ctl(type="__test_event", event=SPEECH_END),
        ]
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    end = transport.first("user_turn_end")
    assert end is not None and end["text"] == "", transport.events


@pytest.mark.asyncio
async def test_resumed_speech_cancels_countdown_same_turn(fake_v2, monkeypatch):
    """말을 멈췄다 이어가면 카운트다운이 취소되고 **같은 턴**이 계속된다.

    학습자가 생각하느라 쉬는 구간이다 — 여기서 턴이 쪼개지면 LLM 이 반쪽 문장을 받는다.
    """
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_SILENCE_MS", 300)
    transport = _StubTransport(
        [
            _ctl(type="start"),
            _ctl(type="__test_event", event=SPEECH_BEGIN),
            _ctl(type="__test_say", text="저는"),
            _ctl(type="__test_event", event=SPEECH_END),
            0.10,  # 임계(300ms) 안에 발화 재개
            _ctl(type="__test_event", event=SPEECH_BEGIN),
            _ctl(type="__test_say", text="학생이에요"),
            _ctl(type="__test_event", event=SPEECH_END),
        ]
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)

    starts = [e for e in transport.events if e["type"] == "user_turn_start"]
    ends = [e for e in transport.events if e["type"] == "user_turn_end"]
    assert len(starts) == 1, transport.events   # 턴이 쪼개지지 않았다
    assert len(ends) == 1, transport.events
    assert ends[0]["text"] == "저는 학생이에요"


@pytest.mark.asyncio
async def test_offset_shortens_wait(fake_v2, monkeypatch):
    """이벤트가 오디오 오프셋을 들고 오면 **이미 흘러간 침묵을 빼고** 남은 만큼만 기다린다.

    이게 없으면 리전 왕복(STT v2 는 서울·도쿄가 없다)과 인식 지연이 침묵 임계에 그대로
    얹혀 턴이 그만큼 늦게 끊긴다.
    """
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_SILENCE_MS", 5000)
    from core.stt import FakeSttV2Stream

    class _OffsetFake(FakeSttV2Stream):
        """speech_end 를 '한참 전 오디오'로 표시 — 이미 침묵이 다 흘렀다는 뜻."""

        def feed_test_event(self, kind: str) -> None:
            if kind == SPEECH_END:
                self._q.put_nowait(SttV2Event(kind=SPEECH_END, offset_ms=0))
                return
            super().feed_test_event(kind)

    monkeypatch.setattr(stt_mod, "make_stt_v2_stream", lambda sr=16000: _OffsetFake())

    # 오디오 10,000ms 를 보낸 뒤(오프셋 0 기준 침묵 10초 경과) speech_end 가 도착 →
    # 남은 대기 0 → 5초 임계에도 불구하고 즉시 닫혀야 한다.
    ten_seconds = b"\x00\x00" * 16000 * 10
    transport = _StubTransport(
        [
            _ctl(type="start"),
            _ctl(type="__test_event", event=SPEECH_BEGIN),
            _ctl(type="__test_say", text="끝났다"),
            CascadeInbound(kind="audio", audio=ten_seconds),
            _ctl(type="__test_event", event=SPEECH_END),
        ]
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=3)  # 5초 임계보다 짧게
    assert transport.first("user_turn_end")["text"] == "끝났다", transport.events


@pytest.mark.asyncio
async def test_ping_pong_and_stop(fake_v2):
    transport = _StubTransport(
        [_ctl(type="start"), _ctl(type="ping", t=7), _ctl(type="stop")], wait_for="pong"
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    assert transport.first("pong")["t"] == 7


@pytest.mark.asyncio
async def test_rollover_is_relayed_and_does_not_break_turn(fake_v2, monkeypatch):
    """롤오버는 진단 이벤트로만 나가고, 진행 중인 턴을 끊지 않는다.

    페이크 스트림에 롤오버 이벤트를 직접 밀어 넣어(실제 스트림 교체 없이) 중계 경로를 본다.
    """
    from core.stt import FakeSttV2Stream

    class _RollingFake(FakeSttV2Stream):
        def feed_test_event(self, kind: str) -> None:
            if kind == "__rollover":
                self._q.put_nowait(
                    SttV2Event(kind=STREAM_ROLLOVER, gap_ms=180, detail="vad_close")
                )
                return
            super().feed_test_event(kind)

    monkeypatch.setattr(stt_mod, "make_stt_v2_stream", lambda sr=16000: _RollingFake())

    transport = _StubTransport(
        [
            _ctl(type="start"),
            _ctl(type="__test_event", event=SPEECH_BEGIN),
            _ctl(type="__test_event", event="__rollover"),
            _ctl(type="__test_say", text="롤오버 뒤에도 이어진다"),
            0.05,
            _ctl(type="__test_event", event=SPEECH_END),
        ]
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)

    roll = transport.first("stt_rollover")
    assert roll is not None and roll["gap_ms"] == 180, transport.events
    # 턴은 롤오버를 사이에 두고도 하나로 유지된다.
    starts = [e for e in transport.events if e["type"] == "user_turn_start"]
    assert len(starts) == 1, transport.events
    assert transport.first("user_turn_end")["text"] == "롤오버 뒤에도 이어진다"


@pytest.mark.asyncio
async def test_immediate_disconnect_is_clean(fake_v2):
    transport = _StubTransport([CascadeInbound(kind="disconnect")])
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    assert transport.events == []


@pytest.mark.asyncio
async def test_stream_error_reports_and_stops(fake_v2, monkeypatch):
    """복구 불가 STT 오류는 error 로 알리고 세션만 끝난다(서버는 계속 — R5)."""
    from core.stt import STREAM_ERROR, FakeSttV2Stream

    class _BrokenFake(FakeSttV2Stream):
        def feed_test_event(self, kind: str) -> None:
            self._q.put_nowait(SttV2Event(kind=STREAM_ERROR, detail="start_failed: boom"))

    monkeypatch.setattr(stt_mod, "make_stt_v2_stream", lambda sr=16000: _BrokenFake())
    transport = _StubTransport(
        [_ctl(type="start"), _ctl(type="__test_event", event=SPEECH_BEGIN)], wait_for="error"
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    err = transport.first("error")
    assert err is not None and err["code"] == "stt_stream_error", transport.events


@pytest.mark.asyncio
async def test_audio_frames_flow_to_stt(fake_v2, monkeypatch):
    """바이너리 프레임은 그대로 STT 로 흘러간다(헤더 없음 — 규격 유지, 클라 제약 #4)."""
    from core.stt import FakeSttV2Stream

    seen: list[bytes] = []

    class _RecordingFake(FakeSttV2Stream):
        async def push_audio(self, pcm: bytes) -> None:
            seen.append(pcm)
            if len(seen) >= 2:
                self._q.put_nowait(SttV2Event(kind=TRANSCRIPT, text="ok", is_final=True))
                self._q.put_nowait(SttV2Event(kind=SPEECH_END))

    monkeypatch.setattr(stt_mod, "make_stt_v2_stream", lambda sr=16000: _RecordingFake())
    transport = _StubTransport(
        [
            _ctl(type="start"),
            CascadeInbound(kind="audio", audio=b"\x01\x02"),
            CascadeInbound(kind="audio", audio=b"\x03\x04"),
        ]
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    assert seen[:2] == [b"\x01\x02", b"\x03\x04"]


# ════════════════ 비버 턴 송출 불변식 (클라 판별식의 근거 — 어기면 클라가 정상 오디오를 버린다)
#
# 클라는 "audio_cancel ~ 다음 turn_start 사이에 도착한 바이너리 = 취소 잔여"로 보고 **전부
# 버린다**. 그 판별이 안전하려면 서버가 아래를 절대 어기면 안 된다. 그래서 테스트로 고정한다.

import pytest as _pytest  # noqa: E402  (아래 블록 전용 별칭)

from domains.learning.realtime.cascade_session import (  # noqa: E402
    BEAVER_BYTES_PER_MS,
    BeaverOutput,
    InvariantError,
)


class _RecordingTransport:
    """나간 이벤트/오디오를 **순서 그대로** 기록한다(순서가 계약의 일부라 순서를 본다)."""

    def __init__(self) -> None:
        self.wire: list[tuple[str, object]] = []

    async def send_event(self, event: dict) -> None:
        self.wire.append(("event", event))

    async def send_audio(self, frame: bytes) -> None:
        self.wire.append(("audio", frame))

    async def receive(self):  # 이 테스트는 송출만 본다
        raise AssertionError("사용하지 않음")

    def kinds(self) -> list[str]:
        return [payload["type"] if kind == "event" else "audio" for kind, payload in self.wire]


def _pcm(ms: int) -> bytes:
    return b"\x00\x00" * int(ms * BEAVER_BYTES_PER_MS / 2)


class _FakeClock:
    """페이싱 검증용 가짜 시계 — 실제로 자지 않고 '잔 시간'만 누적한다."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.t += seconds


@_pytest.mark.asyncio
async def test_i1_no_audio_outside_beaver_turn():
    """I1 — 비버 턴 밖에서 오디오를 보내려 하면 **터진다**(조용히 나가면 클라가 버린다)."""
    out = BeaverOutput(_RecordingTransport())
    with _pytest.raises(InvariantError):
        await out.send(_pcm(100), "안녕")


@_pytest.mark.asyncio
async def test_i2_turn_start_precedes_first_audio_byte():
    """I2 — 모든 비버 턴은 turn_start 로 시작한다(오디오 첫 바이트보다 먼저)."""
    tr = _RecordingTransport()
    clock = _FakeClock()
    out = BeaverOutput(tr, now=clock.now, sleep=clock.sleep)
    await out.begin()
    await out.send(_pcm(100), "안녕하세요")
    await out.end()

    kinds = tr.kinds()
    assert kinds[0] == "turn_start", kinds
    assert kinds.index("turn_start") < kinds.index("audio"), kinds
    # I5 — turn_end 는 마지막 오디오 바이트 뒤에 온다.
    assert kinds[-1] == "turn_end", kinds
    assert kinds.index("turn_end") > max(i for i, k in enumerate(kinds) if k == "audio")


@_pytest.mark.asyncio
async def test_i4_cancelled_turn_has_no_turn_end():
    """I4 — 취소된 턴에는 turn_end 를 보내지 않는다(audio_cancel 이 종결을 겸한다)."""
    tr = _RecordingTransport()
    clock = _FakeClock()
    out = BeaverOutput(tr, now=clock.now, sleep=clock.sleep)
    await out.begin()
    await out.send(_pcm(100), "말하는 중")
    await out.cancel("barge_in")
    await out.end()  # 호출부가 실수로 불러도 나가면 안 된다

    kinds = tr.kinds()
    assert "audio_cancel" in kinds, kinds
    assert "turn_end" not in kinds, kinds


@_pytest.mark.asyncio
async def test_i3_pacing_never_outruns_realtime(monkeypatch):
    """I3 — 누적 송출량 ≤ 실시간 + lead. 서버가 앞서 나가면 클라 버퍼가 무한히 부푼다.

    가짜 시계로 검증한다: 2초 분량을 한 번에 밀어 넣어도, 잔 시간까지 합치면 **실시간을
    넘어서지 않는다**(선행버퍼 lead 만큼만 앞선다).
    """
    import core.config as config_mod

    monkeypatch.setattr(config_mod.settings, "CASCADE_TTS_LEAD_MS", 200)
    tr = _RecordingTransport()
    clock = _FakeClock()
    out = BeaverOutput(tr, now=clock.now, sleep=clock.sleep)
    await out.begin()
    for _ in range(20):                    # 100ms × 20 = 2,000ms 분량
        await out.send(_pcm(100), "가")
    sent_ms = out.sent_bytes / BEAVER_BYTES_PER_MS
    elapsed_ms = clock.t * 1000.0
    assert sent_ms - elapsed_ms <= 200 + 100 + 1e-6, (sent_ms, elapsed_ms)


@_pytest.mark.asyncio
async def test_ledger_truncation_excludes_silence_padding():
    """원장 절단 — 무음 패딩은 바이트 오프셋에 포함되지만 **대사로 세지 않는다**.

    클라는 서버발 무음과 대사를 구분할 수 없어 바이트 수만 보고한다(played_server_bytes).
    분리는 서버 몫이고, 그게 이 원장이다.
    """
    tr = _RecordingTransport()
    clock = _FakeClock()
    out = BeaverOutput(tr, now=clock.now, sleep=clock.sleep)
    await out.begin()
    await out.send(_pcm(500), "안녕하세요")        # 0 ~ 500ms
    await out.send(_pcm(300), "")                  # 500 ~ 800ms  ← 무음 패딩
    await out.send(_pcm(500), "오늘 뭐 했어요")     # 800 ~ 1300ms

    turn = out.turn_id
    all_bytes = out.sent_bytes
    assert out.spoken_text(turn, all_bytes) == "안녕하세요 오늘 뭐 했어요"

    # 패딩 끝(800ms)까지만 들었다 → 첫 문장만 들린 것이다.
    played = int(800 * BEAVER_BYTES_PER_MS)
    assert out.spoken_text(turn, played) == "안녕하세요"

    # 두 번째 문장 중간(1000ms)에서 끊겼다 → **걸친 청크는 버린다**(짧은 쪽 편향).
    played = int(1000 * BEAVER_BYTES_PER_MS)
    assert out.spoken_text(turn, played) == "안녕하세요"


@_pytest.mark.asyncio
async def test_ledger_cancel_sample_adds_stop_lag(monkeypatch):
    """sampled_at='cancel' 이면 실제 정지까지의 지연(50~120ms)만큼 더 들린 것으로 본다."""
    import core.config as config_mod

    monkeypatch.setattr(config_mod.settings, "CASCADE_CANCEL_STOP_MS", 120)
    tr = _RecordingTransport()
    clock = _FakeClock()
    out = BeaverOutput(tr, now=clock.now, sleep=clock.sleep)
    await out.begin()
    await out.send(_pcm(200), "첫문장")     # 0 ~ 200ms
    await out.send(_pcm(200), "둘째문장")   # 200 ~ 400ms

    turn = out.turn_id
    played = int(150 * BEAVER_BYTES_PER_MS)                     # 취소 수신 시점: 150ms
    assert out.spoken_text(turn, played, "cancel") == "첫문장"   # +120ms → 270ms 까지 들림
    assert out.spoken_text(turn, played, "stop") == ""           # 보정 없으면 첫 문장도 미완


# ════════════════ 스트림 롤오버 래퍼 (RollingSttV2Stream) ════════════════
#
# v2 스트림은 5분 한도가 있고, 서버가 임의로 닫을 수도 있다. 상위(세션)가 보는 이벤트 열은
# **하나로 연속**돼야 하고, 그러려면 두 가지가 맞아야 한다:
#   ① 교체 중 들어온 오디오를 버려선 안 된다(버퍼에 담았다 새 스트림에 흘린다)
#   ② 새 스트림의 오디오 오프셋은 0부터 다시 시작한다 → 전역 타임라인으로 **재기준**해야 한다.
#      안 하면 롤오버마다 침묵 계산이 0으로 되돌아가 턴 타이머가 오작동한다.


class _ScriptedStream:
    """GoogleSttV2Stream 흉내 — 지정된 이벤트를 내고 **끝난다**(= 서버가 스트림을 닫음)."""

    def __init__(self, events, hang: bool = False) -> None:
        self._events = list(events)
        self._hang = hang
        self.received: list[bytes] = []
        self.closed = False

    async def start(self) -> None:
        return None

    async def push_audio(self, pcm: bytes) -> None:
        self.received.append(pcm)

    def feed_test(self, text: str) -> None:
        return None

    def feed_test_event(self, kind: str) -> None:
        return None

    async def events(self):
        for ev in self._events:
            yield ev
        if self._hang:                      # 마지막 스트림은 살아 있는 채로 둔다
            await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed = True


@_pytest.mark.asyncio
async def test_rolling_stream_bridges_and_rebases_offsets():
    from core.stt import RollingSttV2Stream

    made: list[_ScriptedStream] = []
    scripts = [
        [SttV2Event(kind=TRANSCRIPT, text="첫", is_final=True, offset_ms=200)],
        [SttV2Event(kind=TRANSCRIPT, text="둘", is_final=True, offset_ms=50)],
    ]

    def factory():
        events = scripts.pop(0) if scripts else []
        stream = _ScriptedStream(events, hang=not scripts)
        made.append(stream)
        return stream

    roller = RollingSttV2Stream(factory, 16000)
    # 스트림이 열리기 전에 들어온 1,000ms — 버려지지 않고 첫 스트림에 리플레이돼야 한다.
    await roller.push_audio(b"\x00\x00" * 16000)

    got: list[SttV2Event] = []

    async def drain():
        async for ev in roller.events():
            got.append(ev)

    task = asyncio.create_task(drain())
    for _ in range(50):                     # 두 스트림이 흐를 때까지 양보
        await asyncio.sleep(0.01)
        if len([e for e in got if e.kind == TRANSCRIPT]) >= 2:
            break
    await roller.close()
    task.cancel()

    texts = [(e.text, e.offset_ms) for e in got if e.kind == TRANSCRIPT]
    assert [t for t, _ in texts] == ["첫", "둘"], got          # 스트림을 넘어 연속된다
    assert texts[0][1] == 200, texts                            # 첫 스트림 base = 0
    # ⭐ 둘째 스트림의 오프셋 50 은 전역 1,000ms + 50 = 1,050 으로 재기준돼야 한다.
    assert texts[1][1] == 1050, texts
    assert any(e.kind == STREAM_ROLLOVER for e in got), got     # 교체 사실은 진단으로 통지
    assert made[0].received == [b"\x00\x00" * 16000]            # 버퍼가 유실 없이 흘러갔다


@_pytest.mark.asyncio
async def test_audio_cancel_carries_turn_id():
    """audio_cancel 은 **turn_id 를 반드시 싣는다** — 클라 회신을 대조할 유일한 열쇠다."""
    tr = _RecordingTransport()
    clock = _FakeClock()
    out = BeaverOutput(tr, now=clock.now, sleep=clock.sleep)
    turn = await out.begin()
    await out.send(_pcm(100), "말하는 중")
    await out.cancel("barge_in")

    cancel = next(p for k, p in tr.wire if k == "event" and p["type"] == "audio_cancel")
    assert cancel["turn_id"] == turn
    assert cancel["epoch"] >= 1
    assert cancel["reason"] == "barge_in"


@_pytest.mark.asyncio
async def test_late_progress_applies_to_its_own_turn_not_the_current_one():
    """⭐ 늦게 도착한 이전 턴 진행도가 **새 턴을 오염시키지 않는다.**

    playback_progress 는 비동기라 서버가 이미 다음 턴을 시작한 뒤 도착할 수 있다.
    원장이 턴별로 살아 있어야 그 턴의 대사만 잘린다.
    """
    tr = _RecordingTransport()
    clock = _FakeClock()
    out = BeaverOutput(tr, now=clock.now, sleep=clock.sleep)

    turn1 = await out.begin()
    await out.send(_pcm(200), "첫턴 앞부분")
    await out.send(_pcm(200), "첫턴 뒷부분")
    await out.cancel()

    turn2 = await out.begin()                      # 서버는 벌써 다음 턴을 시작했다
    await out.send(_pcm(200), "둘째턴 대사")

    # 이제서야 turn1 의 진행도가 도착한다(앞부분까지만 들렸다).
    played = int(200 * BEAVER_BYTES_PER_MS)
    assert out.spoken_text(turn1, played) == "첫턴 앞부분"
    # 같은 바이트 수라도 turn2 원장에 적용하면 다른 결과 — 섞이면 안 되는 이유다.
    assert out.spoken_text(turn2, played) == "둘째턴 대사"


@_pytest.mark.asyncio
async def test_unknown_turn_id_progress_is_ignored():
    """모르는(또는 밀려난) turn_id 는 None → 호출부가 무시한다."""
    tr = _RecordingTransport()
    clock = _FakeClock()
    out = BeaverOutput(tr, now=clock.now, sleep=clock.sleep)
    await out.begin()
    await out.send(_pcm(100), "대사")
    assert out.spoken_text("b999", 10_000) is None


@pytest.mark.asyncio
async def test_session_ignores_estimated_and_stale_progress(fake_v2, caplog):
    """세션 수신부: 추정치(source=estimate)와 미상 turn_id 는 이력에 반영하지 않는다."""
    transport = _StubTransport(
        [
            _ctl(type="start"),
            _ctl(type="playback_progress", turn_id="b1", played_server_bytes=9600,
                 source="estimate", sampled_at="stop"),
            _ctl(type="playback_progress", turn_id="b404", played_server_bytes=9600,
                 source="native", sampled_at="stop"),
            _ctl(type="stop"),
        ],
        wait_for="ready",
    )
    session = CascadeSession(transport)
    await asyncio.wait_for(session.run(), timeout=5)
    assert session._spoken_by_turn == {}, session._spoken_by_turn


# ════════════ [dev 훅] 가짜 비버 오디오 → 취소 배관 (P1 없이 클라 검증용) ════════════
#
# 이 훅이 있는 이유: 서버가 오디오를 낼 일이 없으면 audio_cancel 을 보낼 수가 없고, 그러면
# 클라가 만들어 둔 네이티브 clear() 를 실기기에서 한 줄도 못 돌린다.
# ⭐ 취소는 **버튼 → 서버가 직접 발신**이다. STT 음성감지를 타지 않으므로
#    CASCADE_MIC_ALWAYS_OPEN 이 꺼진 빌드에서도(=AEC 정비 전이라 켤 수 없어도) 잴 수 있다.


class _HookTransport(_StubTransport):
    """스크립트 inbound + 나간 이벤트/오디오를 **순서 그대로** 기록(순서가 계약이다)."""

    def __init__(self, scripted, wait_for="__test_cancel_report") -> None:
        super().__init__(scripted, wait_for=wait_for)
        self.wire: list[tuple[str, object]] = []

    async def send_event(self, event: dict) -> None:
        self.wire.append(("event", event))
        await super().send_event(event)

    async def send_audio(self, frame: bytes) -> None:
        self.wire.append(("audio", frame))

    def wire_kinds(self) -> list[str]:
        return [p["type"] if k == "event" else "audio" for k, p in self.wire]

    def audio_bytes(self) -> int:
        return sum(len(p) for k, p in self.wire if k == "audio")


@pytest.mark.asyncio
async def test_hook_beaver_audio_then_normal_end(fake_v2, monkeypatch):
    """가짜 비버가 흐르고 정상 종료되면 turn_start → 오디오… → turn_end 순서다(I2·I5)."""
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TTS_LEAD_MS", 100_000)  # 테스트에선 페이싱 대기 제거
    transport = _HookTransport(
        [_ctl(type="start"), _ctl(type="__test_beaver", seconds=0.5, tone=False, sentence_ms=100)],
        wait_for="turn_end",
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)

    kinds = transport.wire_kinds()
    assert "turn_start" in kinds, kinds
    assert "audio" in kinds, kinds
    assert kinds.index("turn_start") < kinds.index("audio"), kinds       # I2
    assert kinds.index("turn_end") > max(i for i, k in enumerate(kinds) if k == "audio")  # I5
    # 0.5초 = 5프레임 × 100ms × 4,800B
    assert transport.audio_bytes() == 5 * 4800, transport.audio_bytes()


@pytest.mark.asyncio
async def test_hook_cancel_sends_audio_cancel_without_turn_end(fake_v2, monkeypatch):
    """버튼 취소 → audio_cancel 만 나가고 turn_end 는 안 나간다(I4). **플래그와 무관하다.**

    ⚠ 페이싱을 끄면(lead 를 크게) 30초 스트림이 즉시 끝나버려 취소가 낄 자리가 없다.
    실제 페이싱을 그대로 두고 흐르는 중간에 끊는다 — 실기기 시나리오와 같다.
    """
    monkeypatch.setattr(stt_mod.settings, "CASCADE_MIC_ALWAYS_OPEN", False)  # 꺼도 취소는 돈다
    transport = _HookTransport(
        [
            _ctl(type="start"),
            _ctl(type="__test_beaver", seconds=30, tone=False, sentence_ms=100),
            0.05,
            _ctl(type="__test_cancel", reason="barge_in"),
        ],
        wait_for="audio_cancel",
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)

    kinds = transport.wire_kinds()
    cancel = transport.first("audio_cancel")
    assert cancel is not None, kinds
    assert cancel["turn_id"] == transport.first("turn_start")["turn_id"]
    assert "turn_end" not in kinds, kinds        # I4 — 취소가 턴 종결을 겸한다
    # 30초를 요청했지만 50ms 만에 끊었으므로 실제로 나간 오디오는 lead 언저리뿐이다.
    assert transport.audio_bytes() < 30 * 48000, transport.audio_bytes()


@pytest.mark.asyncio
async def test_hook_cancel_measures_rtt_and_splits_network(fake_v2, monkeypatch):
    """취소 후 진행도가 오면 왕복/클라자체/네트워크로 **분해**해 리포트한다.

    왕복 값에는 네트워크가 섞여 있다 — 클라가 자기 소요를 실어 보내야 갈라진다.
    """
    monkeypatch.setattr(stt_mod.settings, "CASCADE_MIC_ALWAYS_OPEN", False)
    transport = _HookTransport(
        [
            _ctl(type="start"),
            _ctl(type="__test_beaver", seconds=30, tone=False, sentence_ms=100),
            0.05,
            _ctl(type="__test_cancel"),
            0.02,
            _ctl(type="playback_progress", turn_id="b1", played_server_bytes=4800,
                 source="native", sampled_at="stop", client_stop_ms=5),
        ]
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)

    report = transport.first("__test_cancel_report")
    assert report is not None, transport.types()
    assert report["accepted"] is True, report
    assert report["client_stop_ms"] == 5
    assert report["rtt_ms"] >= 0
    assert report["network_ms"] == max(0, report["rtt_ms"] - 5)   # 분해가 실제로 된다


@pytest.mark.asyncio
async def test_hook_reports_ledger_truncation(fake_v2, monkeypatch):
    """진행도 회신 → 리포트에 **원장 절단 결과**가 실린다(안 들린 문장은 빠진다).

    여기서는 취소 없이 1초 스트림을 끝까지 보낸 뒤(원장은 턴 종료 후에도 남는다) 진행도를
    보낸다 — 절단 계산만 따로 본다. 왕복 계측은 위 취소 테스트가 맡는다.
    """
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TTS_LEAD_MS", 100_000)  # 페이싱 대기 제거
    played = 4 * 4800   # 400ms = 문장4까지 들었다
    transport = _HookTransport(
        [
            _ctl(type="start"),
            _ctl(type="__test_beaver", seconds=1.0, tone=False, sentence_ms=100),
            0.05,
            _ctl(type="playback_progress", turn_id="b1", played_server_bytes=played,
                 source="native", sampled_at="stop", client_stop_ms=37),
        ]
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)

    report = transport.first("__test_cancel_report")
    assert report is not None, transport.types()
    assert report["accepted"] is True, report
    assert report["played_server_bytes"] == played
    assert report["sent_bytes"] == 10 * 4800          # 1.0초 = 10프레임
    assert report["unplayed_ms"] == 600               # 6프레임 = 600ms 가 안 들렸다
    # 문장은 100ms 프레임마다 하나씩 끝나므로 400ms 까지면 문장1~4.
    assert report["spoken_text"] == "문장1 문장2 문장3 문장4", report["spoken_text"]
    # 취소를 안 거쳤으니 왕복은 측정되지 않았다 → 분해 불가로 표기한다(-1).
    assert report["network_ms"] == -1, report


@pytest.mark.asyncio
async def test_hook_rejects_estimated_progress_with_reason(fake_v2, monkeypatch):
    """추정치 보고는 리포트에 **거부 사유와 함께** 돌아온다(조용히 무시하지 않는다)."""
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TTS_LEAD_MS", 100_000)
    transport = _HookTransport(
        [
            _ctl(type="start"),
            _ctl(type="__test_beaver", seconds=0.5, tone=False, sentence_ms=100),
            0.05,
            _ctl(type="playback_progress", turn_id="b1", played_server_bytes=4800,
                 source="estimate", sampled_at="stop"),
        ]
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    report = transport.first("__test_cancel_report")
    assert report is not None and report["accepted"] is False, report
    assert "estimate" in report["note"], report["note"]


@pytest.mark.asyncio
async def test_hook_pacing_is_realtime(fake_v2):
    """페이싱이 살아 있다 — 0.6초 분량은 실제로 그만큼 걸려서 나간다(I3).

    산수: 100ms 프레임 6장, 허용 선행 lead=200ms → 앞의 두 장은 그냥 나가고 나머지는
    100ms 씩 기다린다 = **약 0.3초**. 페이싱이 죽으면 0.01초 안에 끝난다.

    ⚠ 임계를 0.3 으로 잡으면 안 된다(2026-08-07 수정). 기대값이 정확히 0.3 이라
    **경계 위**에 앉는 판정이 되는데, asyncio 는 이벤트 루프의 clock resolution 만큼
    일찍 깨어날 수 있고 윈도우에서 그 값이 ~15.6ms 다(sleep 3회 = 최대 ~47ms 이르다).
    실제로 0.297 초가 나와 **HEAD 에서도 5회 중 4회 실패**했다 — 코드가 아니라 판정이
    틀린 것이다. 여기서 지키려는 성질은 "실시간만큼 기다렸나"지 "정확히 300ms 였나"가
    아니므로, 페이싱 유무를 가르는 데 충분한 여유(0.24s)로 잡는다.
    """
    import time as _time

    transport = _HookTransport(
        [_ctl(type="start"), _ctl(type="__test_beaver", seconds=0.6, tone=False, sentence_ms=200)],
        wait_for="turn_end",
    )
    began = _time.monotonic()
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    elapsed = _time.monotonic() - began
    assert elapsed >= 0.24, elapsed


@pytest.mark.asyncio
async def test_hook_stop_measure_missing_is_lower_bound(fake_v2, monkeypatch):
    """⭐ stop_measure 누락 = **하한**으로 표기한다(안 믿는다).

    client_stop_ms 는 값이 항상 오지만 의미가 둘이다 — 하드웨어 잔량까지 빠진 '실제 무음
    시각'이냐, clear() 반환까지만 잰 값이냐. 구분이 없으면 서버가 **가장 낙관적인 값을
    실측으로 믿게 되고**, 그 값으로 "50~120ms 합격"을 내면 실기기에서 뒤집힌다.
    """
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TTS_LEAD_MS", 100_000)
    transport = _HookTransport(
        [
            _ctl(type="start"),
            _ctl(type="__test_beaver", seconds=0.5, tone=False, sentence_ms=100),
            0.05,
            _ctl(type="playback_progress", turn_id="b1", played_server_bytes=4800,
                 source="native", sampled_at="stop", client_stop_ms=42),  # stop_measure 없음
        ]
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    report = transport.first("__test_cancel_report")
    assert report is not None, transport.types()
    assert report["client_stop_ms"] == 42
    assert report["client_stop_is_lower_bound"] is True, report
    assert report["stop_measure"] == "clear_returned", report
    # ⚠ 값 자체는 버리지 않는다 — 쓸모 있는 하한이므로 성격만 표시한다(원장 절단은 그대로).
    assert report["accepted"] is True, report


@pytest.mark.asyncio
async def test_hook_stop_measure_hal_drained_is_taken_as_actual(fake_v2, monkeypatch):
    """hal_drained 로 명시하면 **실제 무음 시각**으로 취급한다(판정에 그대로 쓴다)."""
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TTS_LEAD_MS", 100_000)
    transport = _HookTransport(
        [
            _ctl(type="start"),
            _ctl(type="__test_beaver", seconds=0.5, tone=False, sentence_ms=100),
            0.05,
            _ctl(type="playback_progress", turn_id="b1", played_server_bytes=4800,
                 source="native", sampled_at="stop", client_stop_ms=88,
                 stop_measure="hal_drained"),
        ]
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    report = transport.first("__test_cancel_report")
    assert report is not None and report["client_stop_is_lower_bound"] is False, report
    assert report["stop_measure"] == "hal_drained"
    assert report["client_stop_ms"] == 88


@pytest.mark.asyncio
async def test_hook_report_carries_measurement_context(fake_v2, monkeypatch):
    """측정 맥락(플랫폼·라우트)이 리포트에 그대로 실린다.

    강등률(clear_returned 비율)은 **맥락 없이 읽으면 오독한다** — iOS·타임스탬프 미지원
    라우트는 HAL 잔량을 잴 방법이 없어 100% 강등이 정상이다. 그래서 측정마다 환경을 싣는다
    (라우트는 통화 중에도 바뀌므로 세션이 아니라 측정 단위여야 한다).
    """
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TTS_LEAD_MS", 100_000)
    transport = _HookTransport(
        [
            _ctl(type="start"),
            _ctl(type="__test_beaver", seconds=0.5, tone=False, sentence_ms=100),
            0.05,
            _ctl(type="playback_progress", turn_id="b1", played_server_bytes=4800,
                 source="native", sampled_at="stop", client_stop_ms=64,
                 stop_measure="clear_returned", platform="ios", audio_route="speaker"),
        ]
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    report = transport.first("__test_cancel_report")
    assert report is not None, transport.types()
    assert report["platform"] == "ios" and report["audio_route"] == "speaker", report
    assert report["client_stop_is_lower_bound"] is True   # iOS 는 구조적으로 하한이다


@pytest.mark.asyncio
async def test_hook_unknown_field_values_do_not_drop_the_measurement(fake_v2, monkeypatch):
    """⭐ 모르는 값이 와도 **측정 자체가 사라지지 않는다.**

    source/sampled_at/stop_measure 를 화이트리스트(Literal)로 두면 클라가 값을 하나 늘리는
    순간 pydantic 이 **메시지 전체를 거부**해 리포트도 원장 절단도 통째로 날아간다.
    평문 문자열로 받고 **모르는 값은 보수적인 쪽으로** 해석한다.
    audio_route 도 자유 문자열이라 새 값(receiver)이 그대로 실려 온다.
    """
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TTS_LEAD_MS", 100_000)
    transport = _HookTransport(
        [
            _ctl(type="start"),
            _ctl(type="__test_beaver", seconds=0.5, tone=False, sentence_ms=100),
            0.05,
            _ctl(type="playback_progress", turn_id="b1", played_server_bytes=4800,
                 source="native", sampled_at="stop",
                 stop_measure="hal_drained_estimated",   # ← 아직 없는 값
                 platform="ios", audio_route="receiver"),  # ← 새로 생긴 라우트
        ]
    )
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)

    report = transport.first("__test_cancel_report")
    assert report is not None, transport.types()          # 메시지가 통째로 날아가지 않았다
    assert report["accepted"] is True                     # 원장 절단은 정상 수행
    assert report["audio_route"] == "receiver"            # 새 라우트가 그대로 실린다
    assert report["stop_measure"] == "hal_drained_estimated"
    assert report["client_stop_is_lower_bound"] is True   # 모르는 값 = 안 믿는다(하한)
