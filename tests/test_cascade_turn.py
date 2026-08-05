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
