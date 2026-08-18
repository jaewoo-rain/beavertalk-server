"""PTT(누르고 말하기) 회귀 — **버튼이 턴 경계다.**

2026-08-18 사장님 결정 ⓐ: VAD 를 끄고 PTT 단일 경로로 간다. 그 전환에서 지켜야 할 것이
두 가지이고, 이 파일이 그 둘을 못 박는다.

  ① ⛔ **VAD 경로는 바이트 단위로 불변**이어야 한다. PTT 는 덧붙이기이지 개조가 아니다.
  ② ⛔ **PTT 세션에서는 다섯 시계가 한 번도 안 걸려야 한다**(값 조정이 아니라 경로 미주행):
       벤더 침묵창 300 · 서버 침묵 800 · 발화 재봉합 500 · 전사정지 1500 · STT idle 5000
     남기는 안전망은 `CASCADE_TURN_MAX_S`(30초) 하나다.

⚠ 왜 ②를 이렇게까지 세게 잡나: 실측(2026-08-18 demo-api) 8턴 중 **4턴이 reason=stt_idle**
  이었고 그 턴의 말끝→첫소리가 **7.6초**였다. 시계 하나가 살아남으면 그 실패가 그대로 남는다.
"""

import asyncio
import json
import sys

import pytest

import core.openai_stt as stt_openai
import core.stt as stt_mod
from core.stt import SPEECH_BEGIN, SPEECH_END
from domains.learning.realtime.cascade_session import CascadeInbound, CascadeSession


# ────────────────────────── 어댑터(벤더에 보내는 바이트) ──────────────────────────
class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


async def _connect(monkeypatch, ws: _FakeWS, *, manual_commit: bool):
    """실제 소켓 없이 `start()` 의 **설정 프레임만** 만들어 본다(과금·불안정 0)."""
    monkeypatch.setattr(stt_openai.settings, "GPT_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(stt_openai.settings, "OPENAI_STT_SILENCE_MS", 300)
    stream = stt_openai.OpenAiRealtimeSttStream(
        16000, ["ko-KR"], manual_commit=manual_commit
    )

    class _WSMod:
        @staticmethod
        async def connect(*_a, **_k):
            return ws

    monkeypatch.setitem(sys.modules, "websockets", _WSMod)
    await stream.start()
    if stream._reader is not None:      # 읽기 루프는 필요 없다(가짜 소켓이다)
        stream._reader.cancel()
    return stream


def _turn_detection(ws: _FakeWS):
    hits = [m for m in ws.sent if m.get("type") == "session.update"]
    assert len(hits) == 1, ws.sent
    return hits[0]["session"]["audio"]["input"]["turn_detection"]


@pytest.mark.asyncio
async def test_a_vad_session_still_sends_the_same_turn_detection(monkeypatch):
    """① VAD 세션이 벤더에 보내는 것은 **예전과 글자 하나 같다**."""
    ws = _FakeWS()
    await _connect(monkeypatch, ws, manual_commit=False)
    assert _turn_detection(ws) == {"type": "server_vad", "silence_duration_ms": 300}


@pytest.mark.asyncio
async def test_a_ptt_session_turns_the_vendor_vad_off(monkeypatch):
    """② PTT 는 `turn_detection: null` — 침묵창 300ms 가 **소멸한다**.

    ⭐ 스파이크(20260816_1749 §2)에서 이 모델이 null + 수동 commit 을 에러 0건으로 받는 것을
      실제 응답으로 확인했다. 여기서는 **우리가 그렇게 보내는지**만 지킨다.
    """
    ws = _FakeWS()
    await _connect(monkeypatch, ws, manual_commit=True)
    assert _turn_detection(ws) is None, ws.sent


@pytest.mark.asyncio
async def test_commit_is_only_sent_in_ptt(monkeypatch):
    """⛔ VAD 세션에서 commit 이 나가면 벤더가 item 을 더 만들어 기존 전사 개수가 바뀐다."""
    ws = _FakeWS()
    vad = await _connect(monkeypatch, ws, manual_commit=False)
    assert await vad.commit() is False
    assert not [m for m in ws.sent if m.get("type") == "input_audio_buffer.commit"]

    ws2 = _FakeWS()
    ptt = await _connect(monkeypatch, ws2, manual_commit=True)
    assert await ptt.commit() is True
    assert [m for m in ws2.sent if m.get("type") == "input_audio_buffer.commit"]


@pytest.mark.asyncio
async def test_the_empty_final_is_surfaced_only_in_ptt(monkeypatch):
    """빈 전사도 PTT 에서는 **올려야** 한다 — 안 올리면 상한만큼 헛대기한다.

    ⛔ VAD 경로는 예전 그대로 버린다(빈 final 이 새로 흐르면 턴 판정이 바뀐다).
    """
    empty = {"type": "conversation.item.input_audio_transcription.completed",
             "item_id": "i1", "transcript": ""}
    vad = await _connect(monkeypatch, _FakeWS(), manual_commit=False)
    assert vad._translate(dict(empty)) == []

    ptt = await _connect(monkeypatch, _FakeWS(), manual_commit=True)
    events = ptt._translate(dict(empty))
    assert len(events) == 1 and events[0].is_final and events[0].text == ""


@pytest.mark.asyncio
async def test_the_commit_roundtrip_is_measured_with_its_audio_length(monkeypatch, caplog):
    """⭐⭐ 가설 A/B 를 가를 계기판 — **왕복 ms 와 그 턴 오디오 길이가 같은 줄**에 있어야 한다.

    가설A 흘리며 전사 → 길이와 무관하게 짧다   ⇒ 목표 말끝→첫소리 1.8초 달성
    가설B commit 뒤 일괄 → 길이에 비례한다     ⇒ 다음 표적은 LLM 이 아니라 STT 벤더 교체
    ⛔ 두 값이 갈라져 찍히면 사람이 손으로 짝을 맞춰야 하고, 그러면 아무도 안 본다.
    """
    stream = await _connect(monkeypatch, _FakeWS(), manual_commit=True)
    stream._sent_ms = 2000.0                 # 이 hold 로 2초를 흘렸다
    with caplog.at_level("INFO", logger="core.openai_stt"):
        assert await stream.commit() is True
        stream._translate({"type": "conversation.item.input_audio_transcription.completed",
                           "item_id": "i1", "transcript": "안녕하세요"})
    assert stream.last_commit_lag_ms >= 0
    assert stream.last_commit_audio_ms == 2000
    line = [r.getMessage() for r in caplog.records if "commit" in r.getMessage()]
    assert line and "이 턴 오디오 2000ms" in line[0], caplog.text


# ────────────────────────────── 세션(턴 상태기계) ──────────────────────────────
@pytest.fixture
def fake_v2(monkeypatch):
    monkeypatch.setattr(stt_mod.settings, "STT_V2_FAKE", True)
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_SILENCE_MS", 60)
    stt_mod.get_speech_v2_client.cache_clear()
    yield
    stt_mod.get_speech_v2_client.cache_clear()


class _StubTransport:
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
            if isinstance(item, float):
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


# 16kHz·PCM16 에서 200ms = 6,400 바이트. 최소 hold(100ms)를 넉넉히 넘긴다.
def _hold_audio() -> CascadeInbound:
    return CascadeInbound(kind="audio", audio=b"\x00" * 6400)


def _ptt_script(*extra):
    return [
        _ctl(type="start", sampleRate=16000, turnControl="ptt"),
        _ctl(type="ptt_press"),
        _hold_audio(),
        _ctl(type="ptt_release"),
        *extra,
    ]


@pytest.mark.asyncio
async def test_a_ptt_turn_opens_on_press_and_closes_on_release(fake_v2):
    """버튼이 턴을 열고 닫는다 — 사유는 **ptt_release** 로 로그에서 갈린다."""
    transport = _StubTransport(_ptt_script(_ctl(type="__test_say", text="안녕하세요")))
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)

    assert "user_turn_start" in transport.types(), transport.events
    end = transport.first("user_turn_end")
    assert end is not None, transport.events
    assert end["text"] == "안녕하세요"
    assert end["reason"] == "ptt_release", end
    # ⭐ speech_ms 가 **릴리즈 기준**으로 채워진다. VAD 이벤트가 없다고 0 이 되면
    #   통화 기록과 A/B 기준선(플랜 §8)이 조용히 죽는다.
    assert end["speech_ms"] >= 0


@pytest.mark.asyncio
async def test_ptt_never_arms_the_vad_timers(fake_v2, monkeypatch):
    """⛔⛔ **다섯 시계가 한 번도 안 걸린다** — 이 파일의 존재 이유다.

    값이 커서 안 걸리는 게 아니라 **경로를 안 탄다**를 증명한다. 살아 있으면서 아무것도
    안 지키는 코드가 제일 나쁘다는 게 이번 결정의 전제다.
    """
    armed: list[str] = []
    real_close = CascadeSession._arm_close_timer
    real_merge = CascadeSession._is_same_utterance

    def spy_close(self, event, silence_ms=None):
        armed.append("close")
        return real_close(self, event, silence_ms)

    def spy_merge(self, event, prev_offset_ms):
        armed.append("merge")
        return real_merge(self, event, prev_offset_ms)

    monkeypatch.setattr(CascadeSession, "_arm_close_timer", spy_close)
    monkeypatch.setattr(CascadeSession, "_is_same_utterance", spy_merge)

    transport = _StubTransport(_ptt_script(_ctl(type="__test_say", text="안녕하세요")))
    session = CascadeSession(transport)
    await asyncio.wait_for(session.run(), timeout=5)

    assert armed == [], "PTT 인데 VAD 시계가 걸렸다: %r" % armed
    assert session._turn_idle_at is None      # STT idle 5초 — 쥐고 있는 턴을 닫던 그 시계
    assert session._close_at is None          # 서버 침묵 800ms
    assert session._ptt_final_at is None      # 릴리즈 안전망도 정상 경로에선 안 남는다


@pytest.mark.asyncio
async def test_vad_sessions_still_arm_the_silence_timer(fake_v2, monkeypatch):
    """대조군 — 같은 스파이로 **VAD 세션은 여전히 시계를 건다**(위 시험이 공허하지 않다)."""
    armed: list[str] = []
    real_close = CascadeSession._arm_close_timer

    def spy_close(self, event, silence_ms=None):
        armed.append("close")
        return real_close(self, event, silence_ms)

    monkeypatch.setattr(CascadeSession, "_arm_close_timer", spy_close)
    transport = _StubTransport([
        _ctl(type="start", sampleRate=16000),
        _ctl(type="__test_event", event=SPEECH_BEGIN),
        _ctl(type="__test_say", text="안녕하세요"),
        0.05,
        _ctl(type="__test_event", event=SPEECH_END),
    ])
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    assert armed, "VAD 세션인데 침묵 타이머가 안 걸렸다 — 대조군이 무너졌다"


@pytest.mark.asyncio
async def test_a_short_press_sends_no_commit(fake_v2):
    """눌렀다 곧바로 뗐다 — **정상 조작**이다. commit 을 보내면 헛대기 1.5초가 붙는다."""
    transport = _StubTransport([
        _ctl(type="start", sampleRate=16000, turnControl="ptt"),
        _ctl(type="ptt_press"),
        _ctl(type="ptt_release"),          # 오디오 0바이트 = hold 0ms
    ])
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    end = transport.first("user_turn_end")
    assert end is not None and end["reason"] == "ptt_short", transport.events
    assert end["text"] == ""


@pytest.mark.asyncio
async def test_a_late_transcript_does_not_open_a_ghost_turn(fake_v2):
    """⛔ PTT 에서 **전사는 턴을 못 연다** — 유령 턴이 구조적으로 불가능해진다."""
    transport = _StubTransport(_ptt_script(
        _ctl(type="__test_say", text="안녕하세요"),
        0.05,
        _ctl(type="__test_say", text="늦게 온 전사"),
        0.05,
    ))
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    starts = [e for e in transport.events if e.get("type") == "user_turn_start"]
    assert len(starts) == 1, transport.events


@pytest.mark.asyncio
async def test_vad_events_from_a_fallback_engine_are_ignored(fake_v2):
    """⛔ 구글 폴백은 VAD 이벤트를 계속 보낸다 — 타면 버튼과 **두 주인**이 된다."""
    transport = _StubTransport(_ptt_script(
        _ctl(type="__test_event", event=SPEECH_END),   # 폴백 엔진의 잔여 VAD
        0.05,
        _ctl(type="__test_say", text="안녕하세요"),
    ))
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    end = transport.first("user_turn_end")
    assert end is not None and end["reason"] == "ptt_release", transport.events


@pytest.mark.asyncio
async def test_an_unknown_turn_control_falls_back_to_vad(fake_v2):
    """⛔ 모르는 값 하나로 start 전체가 거부되면 안 된다 — 보수적인 쪽(vad)으로 읽는다."""
    transport = _StubTransport([
        _ctl(type="start", sampleRate=16000, turnControl="PUSH_TO_TALK_V2"),
        _ctl(type="__test_event", event=SPEECH_BEGIN),
        _ctl(type="__test_say", text="안녕하세요"),
        0.05,
        _ctl(type="__test_event", event=SPEECH_END),
    ])
    await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    end = transport.first("user_turn_end")
    assert end is not None and end["reason"] == "silence", transport.events
