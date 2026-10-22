"""PM-DEC-998 — 상시 배경 말소리(TV)에서 무응답 대책: (A) 소리 미룸 상한 · (C) «시끄럽다» 안내 · (B) audio_stream_end 측정."""
from __future__ import annotations

import asyncio

import pytest

import domains.learning.realtime.call_session as cs


class _Sess:
    def __init__(self):
        self.text_turns: list[str] = []
        self.stream_ends = 0

    async def send_text_turn(self, text):
        self.text_turns.append(text)

    async def send_audio_stream_end(self):
        self.stream_ends += 1


def _state(now, *, since=40.0):
    st = cs._CallState()
    st.learner_spoke = True
    st.call_start_ts = now - 100
    st.beaver_end_ts = now - since
    st.last_activity_ts = now - since
    st.user_voice_ts = now - 0.1      # 소리는 계속 온다(TV)
    st.ambient_ema = 0.9              # PM-DEC-1005 — 배경 소리 비율(낮은 문턱)
    st.ambient_ts = now - 0.1
    st.user_turn_open = False         # 전사 없음
    return st


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(cs, "NOISE_DEFER_CAP_S", 12.0)
    monkeypatch.setattr(cs, "NOISE_NOTE_S", 30.0)
    monkeypatch.setattr(cs, "NOISE_STREAM_END_S", 0.0)


@pytest.mark.asyncio
async def test_note_injected_after_30s_without_transcript(on):
    now = asyncio.get_running_loop().time()
    st, s = _state(now), _Sess()
    v, noted = await cs._noise_guard(s, st, st.user_voice_ts)
    assert noted and len(s.text_turns) == 1 and "시끄러운" in s.text_turns[0]
    assert st.noise_note_count == 1
    v2, noted2 = await cs._noise_guard(s, st, st.user_voice_ts)   # 안내 직후 다시 넣지 않는다
    assert not noted2 and len(s.text_turns) == 1


@pytest.mark.asyncio
async def test_note_capped_per_call(on):
    now = asyncio.get_running_loop().time()
    st, s = _state(now), _Sess()
    st.noise_note_count = cs.NOISE_NOTE_MAX
    _, noted = await cs._noise_guard(s, st, st.user_voice_ts)
    assert not noted and not s.text_turns


@pytest.mark.asyncio
async def test_defer_cap_stops_voice_postponing_silence_clock(on):
    now = asyncio.get_running_loop().time()
    st, s = _state(now, since=15.0), _Sess()
    v, noted = await cs._noise_guard(s, st, st.user_voice_ts)
    assert not noted and v == 0.0 and not s.text_turns


@pytest.mark.asyncio
async def test_transcript_means_learner_is_talking_no_change(on):
    """전사가 있으면(학습자가 실제로 말하는 중 · PM-DEC-891) 종전대로 소리가 무음 시계를 미룬다."""
    now = asyncio.get_running_loop().time()
    st, s = _state(now), _Sess()
    st.user_turn_open = True
    v, noted = await cs._noise_guard(s, st, st.user_voice_ts)
    assert not noted and v == st.user_voice_ts and not s.text_turns


@pytest.mark.asyncio
async def test_excluded_leveltest_closing_and_before_first_words(on):
    now = asyncio.get_running_loop().time()
    for attr, val in (("is_leveltest", True), ("close_seed_sent", True), ("should_close", True), ("learner_spoke", False)):
        st, s = _state(now), _Sess()
        setattr(st, attr, val)
        v, noted = await cs._noise_guard(s, st, st.user_voice_ts)
        assert not noted and v == st.user_voice_ts and not s.text_turns, attr


@pytest.mark.asyncio
async def test_quiet_room_is_ordinary_silence(on):
    """소리도 안 오면(진짜 무음) 안내는 없다 — 종전 무음 넛지 몫이다."""
    now = asyncio.get_running_loop().time()
    st, s = _state(now), _Sess()
    st.user_voice_ts = now - 35.0
    st.ambient_ema = 0.0              # 조용한 방
    _, noted = await cs._noise_guard(s, st, st.user_voice_ts)
    assert not noted and not s.text_turns


@pytest.mark.asyncio
async def test_stream_end_measure_once(monkeypatch):
    monkeypatch.setattr(cs, "NOISE_DEFER_CAP_S", 0.0)
    monkeypatch.setattr(cs, "NOISE_NOTE_S", 0.0)
    monkeypatch.setattr(cs, "NOISE_STREAM_END_S", 12.0)
    now = asyncio.get_running_loop().time()
    st, s = _state(now, since=13.0), _Sess()
    await cs._noise_guard(s, st, st.user_voice_ts)
    await cs._noise_guard(s, st, st.user_voice_ts)
    assert s.stream_ends == 1


def test_off_by_default():
    assert cs.NOISE_DEFER_CAP_S == 0 and cs.NOISE_NOTE_S == 0 and cs.NOISE_STREAM_END_S == 0 and cs.MIC_RMS_LOG is False


@pytest.mark.asyncio
async def test_reply_to_noise_note_is_not_held(monkeypatch):
    monkeypatch.setattr(cs, "REPLY_HOLD_S", 1.5)
    st = cs._CallState()
    st.learner_spoke = True
    assert cs._reply_hold_eligible(st)
    st.noise_note_ts = asyncio.get_running_loop().time()
    assert not cs._reply_hold_eligible(st)


def _pcm(level, n=320):
    return int(level * 32767).to_bytes(2, "little", signed=True) * n


@pytest.mark.asyncio
async def test_tv_below_voice_threshold_still_counts_as_sound(on):
    """PM-DEC-1005 — TV 실측 RMS 0.006(목소리 문턱 0.02 아래)도 배경 소리로 잡혀 «시끄럽다» 안내가 나간다."""
    now = asyncio.get_running_loop().time()
    st, s = _state(now), _Sess()
    st.user_voice_ts = None
    st.ambient_ema = 0.0
    for _ in range(300):
        cs._ambient_track(st, _pcm(0.006))
    assert st.ambient_ema >= cs.NOISE_AMBIENT_RATIO
    _, noted = await cs._noise_guard(s, st, 0.0)
    assert noted and len(s.text_turns) == 1


@pytest.mark.asyncio
async def test_quiet_room_low_residual_is_not_noise(on):
    now = asyncio.get_running_loop().time()
    st, s = _state(now), _Sess()
    st.ambient_ema = 0.0
    for _ in range(300):
        cs._ambient_track(st, _pcm(0.001))
    assert st.ambient_ema < cs.NOISE_AMBIENT_RATIO
    _, noted = await cs._noise_guard(s, st, 0.0)
    assert not noted and not s.text_turns


@pytest.mark.asyncio
async def test_noise_gate_zeroes_low_frames_keeps_voice_and_tail(monkeypatch):
    monkeypatch.setattr(cs, "NOISE_GATE_RMS", 0.012)
    st = cs._CallState()
    tv, voice = _pcm(0.008), _pcm(0.05)
    assert cs._noise_gate(st, tv) == bytes(len(tv))          # 작은 소리 → 무음
    assert cs._noise_gate(st, voice) == voice                # 목소리 그대로
    assert cs._noise_gate(st, tv) == tv                      # 목소리 직후 말꼬리 보호
    st.gate_voice_ts -= 1.0
    assert cs._noise_gate(st, tv) == bytes(len(tv))
    assert st.gate_zeroed == 2
    mid = _pcm(0.015)
    st.gate_voice_ts -= 1.0
    assert cs._noise_gate(st, mid) == mid                    # 게이트 문턱 위는 그대로


def test_noise_floor_does_not_stick_at_zero():
    st = cs._CallState()

    async def _go():
        for _ in range(50):
            cs._reply_hold_track_voice(st, _pcm(0.006))
        cs._reply_hold_track_voice(st, bytes(640))           # 디지털 무음 한 프레임
        return st.hold_noise_floor

    assert asyncio.run(_go()) > 0.004


def test_gate_off_by_default():
    assert cs.NOISE_GATE_RMS == 0
