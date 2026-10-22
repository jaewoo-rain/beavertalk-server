"""PM-DEC-993·994 — 답 보류(서버가 비버 답을 잠깐 붙잡아 둔다) + 해제 뒤 마이크 닫기.

펌프(_pump_gemini_to_client)를 시간 있는 가짜 Live 세션으로 돌려 앱으로 나간 프레임을 본다.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import domains.learning.realtime.call_session as cs
from core.config import settings as app_settings
from core.gemini_live import LiveEvent


class _WS:
    def __init__(self):
        self.log: list = []        # (시각, "text"/"bytes", 값) — 순서·시각 확인용
        self.t0 = asyncio.get_running_loop().time()

    async def send_text(self, text):
        self.log.append((asyncio.get_running_loop().time() - self.t0, "text", json.loads(text)))

    async def send_bytes(self, data):
        self.log.append((asyncio.get_running_loop().time() - self.t0, "bytes", data))

    def types(self, faces=False):
        # 턴 시작마다 나가는 표정 초기화(sentence)는 기본으로 뺀다 — 보는 것은 턴·자막·오디오 순서다
        return [v.get("type") if k == "text" else "audio" for _, k, v in self.log
                if faces or not (k == "text" and v.get("type") == "sentence")]

    def audio(self):
        return [v for _, k, v in self.log if k == "bytes"]


class _TimedSession:
    """(지연초, LiveEvent | 콜러블) 목록을 시간대로 흘린다. 콜러블은 그 시각에 상태를 바꾼다(학습자 목소리 흉내)."""

    def __init__(self, script, tail_s=2.0):
        self._script = list(script)
        self._tail = tail_s
        self.sent_text_turns: list[str] = []
        self.tool_responses: list = []

    async def send_audio(self, pcm16_16k: bytes) -> None: ...

    async def send_text_turn(self, text: str) -> None:
        self.sent_text_turns.append(text)

    async def send_tool_response(self, *a, **k) -> None:
        self.tool_responses.append((a, k))

    async def events(self):
        for delay, ev in self._script:
            if delay:
                await asyncio.sleep(delay)
            if callable(ev):
                ev()
                continue
            yield ev
        await asyncio.sleep(self._tail)   # 보류 해제가 끝날 시간을 준다(실통화에선 스트림이 이어진다)


@pytest.fixture
def hold_on(monkeypatch):
    monkeypatch.setattr(cs, "REPLY_HOLD_S", 0.5)
    monkeypatch.setattr(cs, "REPLY_HOLD_MAX_S", 1.2)
    monkeypatch.setattr(cs, "REPLY_HOLD_MUTE_TAIL_S", 1.0)


def _state(spoke=True):
    st = cs._CallState()
    st.learner_spoke = spoke
    return st


def _voice(st):
    def _f():
        st.hold_voice_ts = asyncio.get_running_loop().time()
    return _f


async def _run(st, script, tail_s=2.0):
    ws = _WS()
    with pytest.raises(cs._CallFinished):
        await cs._pump_gemini_to_client(ws, _TimedSession(script, tail_s), st)
    return ws


@pytest.mark.asyncio
async def test_held_until_learner_quiet(hold_on):
    """학습자가 방금 말했으면 조용 0.5초가 될 때까지 turn_start·자막·오디오를 안 보낸다. 그 뒤 원래 순서 그대로."""
    st = _state()
    ws_holder = {}
    script = [
        (0, _voice(st)),
        (0, LiveEvent(kind="out_tr", text="좋아요")),
        (0, LiveEvent(kind="audio", audio=b"\x01\x00" * 10)),
        (0, LiveEvent(kind="turn_end")),
    ]
    ws = await _run(st, script)
    ws_holder["ws"] = ws
    assert ws.types() == ["turn_start", "output_transcript", "audio", "turn_end"]
    first = ws.log[0][0]   # 첫 프레임(turn_start) 시각
    assert 0.45 <= first <= 0.8, first
    assert st.turn_id is None


@pytest.mark.asyncio
async def test_no_delay_when_learner_already_quiet(hold_on):
    """학습자가 이미 충분히 조용했으면 지연 0 — 붙잡자마자 바로 내보낸다."""
    st = _state()
    st.hold_voice_ts = asyncio.get_running_loop().time() - 5.0
    ws = await _run(st, [
        (0, LiveEvent(kind="audio", audio=b"\x01\x00" * 10)),
        (0, LiveEvent(kind="turn_end")),
    ])
    assert ws.types() == ["turn_start", "audio", "turn_end"]
    assert ws.log[0][0] < 0.1


@pytest.mark.asyncio
async def test_interrupted_hold_is_discarded_and_late_turn_end_swallowed(hold_on):
    """학습자가 이어 말해 Live 가 답을 버리면 그 답은 앱에 한 조각도 안 간다 · 늦은 turn_end 도 삼킨다 · 다음 답은 정상."""
    st = _state()
    script = [
        (0, _voice(st)),
        (0, LiveEvent(kind="audio", audio=b"\xAA\x00" * 10)),   # 문장 중간 쉼에 시작한 답
        (0.1, _voice(st)),
        (0, LiveEvent(kind="interrupted")),
        (0, LiveEvent(kind="turn_end")),
        (0.1, LiveEvent(kind="in_tr", text="더 말했어요")),
        (0.6, LiveEvent(kind="audio", audio=b"\xBB\x00" * 10)),   # 전체 문장 뒤의 답
        (0, LiveEvent(kind="turn_end")),
    ]
    ws = await _run(st, script)
    assert b"\xAA\x00" * 10 not in ws.audio()
    assert ws.audio() == [b"\xBB\x00" * 10]
    t = [x for x in ws.types() if x != "input_transcript"]
    assert t == ["turn_start", "audio", "turn_end"], t


@pytest.mark.asyncio
async def test_resumed_after_complete_answer_is_discarded(hold_on):
    """Live 가 답을 다 만든 뒤 학습자가 이어 말하면(목소리 + 전사) 그 답을 버린다 — 이어진 말의 답과 겹치지 않게."""
    st = _state()
    script = [
        (0, _voice(st)),
        (0, LiveEvent(kind="audio", audio=b"\xAA\x00" * 10)),
        (0, LiveEvent(kind="turn_end")),
        (0.25, _voice(st)),
        (0, LiveEvent(kind="in_tr", text="그리고요")),
        (0.7, LiveEvent(kind="audio", audio=b"\xBB\x00" * 10)),
        (0, LiveEvent(kind="turn_end")),
    ]
    ws = await _run(st, script)
    assert ws.audio() == [b"\xBB\x00" * 10]


@pytest.mark.asyncio
async def test_late_transcript_piece_is_not_resume(hold_on):
    """3.x 는 학습자 전사 끝 조각을 답과 같이 보낸다 — 보류 직후 전사만 온 것(새 목소리 없음)은 이어 말함이 아니다."""
    st = _state()
    script = [
        (0, _voice(st)),
        (0, LiveEvent(kind="audio", audio=b"\xAA\x00" * 10)),
        (0, LiveEvent(kind="in_tr", text="요")),
        (0, LiveEvent(kind="turn_end")),
    ]
    ws = await _run(st, script)
    assert ws.audio() == [b"\xAA\x00" * 10]


@pytest.mark.asyncio
async def test_continuous_noise_releases_at_max(hold_on):
    """소음이 계속돼 조용해지지 않아도 최대 보류(1.2초)에서 내보낸다 — 영원히 붙잡지 않는다."""
    st = _state()
    script = [(0, _voice(st)), (0, LiveEvent(kind="audio", audio=b"\x01\x00" * 10)), (0, LiveEvent(kind="turn_end"))]
    script += [(0.1, _voice(st)) for _ in range(20)]
    ws = await _run(st, script, tail_s=0.5)
    assert ws.types()[:3] == ["turn_start", "audio", "turn_end"]
    assert 1.15 <= ws.log[0][0] <= 1.5, ws.log[0][0]


@pytest.mark.asyncio
async def test_greeting_not_held(hold_on):
    """첫 인사(학습자 말 전)는 보류 대상이 아니다 — 종전대로 바로 보낸다."""
    st = _state(spoke=False)
    st.hold_voice_ts = asyncio.get_running_loop().time()
    ws = await _run(st, [(0, LiveEvent(kind="audio", audio=b"\x01\x00" * 10))], tail_s=0.3)
    assert ws.types()[:2] == ["turn_start", "audio"]
    assert ws.log[0][0] < 0.1


@pytest.mark.asyncio
async def test_closing_and_leveltest_not_held(hold_on):
    for attr in ("should_close", "is_leveltest"):
        st = _state()
        setattr(st, attr, True)
        st.hold_voice_ts = asyncio.get_running_loop().time()
        ws = await _run(st, [(0, LiveEvent(kind="audio", audio=b"\x01\x00" * 10))], tail_s=0.3)
        assert ws.log and ws.log[0][0] < 0.1, attr


@pytest.mark.asyncio
async def test_mic_muted_until_estimated_playback_end(hold_on):
    """해제된 답의 turn_end 뒤에도 추정 재생 끝(해제 + 오디오 길이 + 여유)까지 마이크를 Live 로 안 보낸다."""
    st = _state()
    st.hold_voice_ts = asyncio.get_running_loop().time() - 5.0
    ws = await _run(st, [
        (0, LiveEvent(kind="audio", audio=bytes(48000))),   # 1초 분량
        (0, LiveEvent(kind="turn_end")),
    ], tail_s=0.2)
    assert ws.types() == ["turn_start", "audio", "turn_end"]
    now = asyncio.get_running_loop().time()
    assert st.reply_mute_until - st.hold_release_ts == pytest.approx(2.0, abs=0.01)
    assert cs._reply_muted(st) and st.reply_mute_until > now


@pytest.mark.asyncio
async def test_face_marker_held_then_sent_before_audio(hold_on, monkeypatch):
    """보류 중 온 표정 마커는 붙잡았다가 해제 때 오디오 앞에 흘린다 · 도구 응답은 Live 에 바로 간다."""
    monkeypatch.setattr(app_settings, "LIVE_FACE_SPIKE", True, raising=False)
    st = _state()
    sess_script = [
        (0, _voice(st)),
        (0, LiveEvent(kind="tool_call", fn_name="set_face", fn_id="fc1", fn_args={"emotion": "happy"})),
        (0, LiveEvent(kind="audio", audio=b"\x01\x00" * 10)),
        (0, LiveEvent(kind="turn_end")),
    ]
    ws = _WS()
    sess = _TimedSession(sess_script)
    with pytest.raises(cs._CallFinished):
        await cs._pump_gemini_to_client(ws, sess, st)
    assert sess.tool_responses, "도구 응답이 Live 에 안 갔다"
    t = ws.types(faces=True)
    marks = [i for i, (_, k, v) in enumerate(ws.log) if k == "text" and v.get("type") == "sentence" and v.get("emotion") == "happy"]
    assert marks, t
    assert marks[0] < t.index("audio"), t
    assert ws.log[marks[0]][0] >= 0.45


@pytest.mark.asyncio
async def test_off_is_unchanged(monkeypatch):
    """기본(0)은 종전 경로 — 학습자가 방금 말했어도 바로 보낸다."""
    monkeypatch.setattr(cs, "REPLY_HOLD_S", 0.0)
    st = _state()
    st.hold_voice_ts = asyncio.get_running_loop().time()
    ws = await _run(st, [(0, LiveEvent(kind="audio", audio=b"\x01\x00" * 10)), (0, LiveEvent(kind="turn_end"))], tail_s=0.1)
    assert ws.types() == ["turn_start", "audio", "turn_end"]
    assert ws.log[0][0] < 0.1
    assert st.reply_mute_until == 0.0


def test_voice_tracker_adapts_to_steady_noise(monkeypatch):
    """TV 같은 고른 소음이 계속되면 바닥이 올라가 결국 «목소리» 로 안 센다."""
    st = cs._CallState()
    loud = (int(0.05 * 32767)).to_bytes(2, "little", signed=True) * 320

    async def _go():
        for _ in range(2000):
            cs._reply_hold_track_voice(st, loud)
        st.hold_voice_ts = None
        cs._reply_hold_track_voice(st, loud)
        return st.hold_voice_ts

    assert asyncio.run(_go()) is None
    assert st.hold_noise_floor > 0.03
