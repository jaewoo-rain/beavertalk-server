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


@pytest.mark.asyncio
async def test_face_only_hold_then_self_cancel_sends_nothing(hold_on, monkeypatch):
    """set_face 뒤 소리 없이 Live 가 스스로 버리면(빈 생성 · hold1500 2470) 표정 마커도 앱에 안 간다 — 학습자가 조용해도 답이 올 때까지 둔다."""
    monkeypatch.setattr(app_settings, "LIVE_FACE_SPIKE", True, raising=False)
    st = _state()
    st.hold_voice_ts = asyncio.get_running_loop().time() - 5.0   # 학습자는 이미 조용하다
    sess = _TimedSession([
        (0, LiveEvent(kind="tool_call", fn_name="set_face", fn_id="fc1", fn_args={"emotion": "sad"})),
        (0.3, LiveEvent(kind="interrupted")),
        (0.6, LiveEvent(kind="audio", audio=b"\x01\x00" * 10)),
        (0, LiveEvent(kind="turn_end")),
    ])
    ws = _WS()
    with pytest.raises(cs._CallFinished):
        await cs._pump_gemini_to_client(ws, sess, st)
    sad = [v for _, k, v in ws.log if k == "text" and v.get("type") == "sentence" and v.get("emotion") == "sad"]
    assert not sad, "버린 답의 표정이 나갔다"
    assert ws.types() == ["turn_start", "audio", "turn_end"]


@pytest.mark.asyncio
async def test_bare_empty_generation_turn_end_not_sent(hold_on):
    """출력 0 으로 버린 빈 생성(«열린 턴 없음 · 0B» · 2475)의 빈 turn_end 는 앱에 안 보낸다 — 다음 답은 정상."""
    st = _state()
    st.hold_voice_ts = asyncio.get_running_loop().time() - 5.0
    ws = await _run(st, [
        (0, LiveEvent(kind="interrupted")),
        (0, LiveEvent(kind="turn_end")),
        (0.2, LiveEvent(kind="audio", audio=b"\x01\x00" * 10)),
        (0, LiveEvent(kind="turn_end")),
    ])
    assert ws.types() == ["turn_start", "audio", "turn_end"]


@pytest.mark.asyncio
async def test_empty_turn_end_kept_while_closing(hold_on):
    """종료 진행 중에는 빈 turn_end 를 삼키지 않는다(작별 판정 경로를 그대로 둔다)."""
    st = _state()
    st.close_seed_sent = True
    ws = await _run(st, [
        (0, LiveEvent(kind="interrupted")),
        (0, LiveEvent(kind="turn_end")),
    ], tail_s=0.2)
    assert "turn_end" in ws.types()


def test_empty_generation_is_not_mute_greeting():
    """2488 — 학습자 첫 답 전사가 오기 전 출력 0 빈 생성의 turn_end 를 벙어리 인사로 보지 않는다(전사도 소리도 없다)."""
    st = cs._CallState()
    st.session_epoch = 1
    st.beaver_turns = 1
    st.seed_text = "선톡"
    assert cs._greeting_was_mute(st, 0) is True                 # 종전 호출(전사 정보 없음)은 그대로
    assert cs._greeting_was_mute(st, 0, "") is False            # 빈 생성 — 재시드 안 함
    assert cs._greeting_was_mute(st, 0, "안녕, 오늘 어땠어?") is True   # 진짜 벙어리 인사(전사만 있음)


@pytest.mark.asyncio
async def test_hold_eligible_on_voice_before_transcript(monkeypatch):
    """학습자 전사가 늦어도 인사 뒤 목소리가 났으면 답 보류 대상이다(2488 t2 가 보류 없이 끊었다)."""
    monkeypatch.setattr(cs, "REPLY_HOLD_S", 1.5)
    st = cs._CallState()
    assert not cs._reply_hold_eligible(st)          # 인사 전
    st.beaver_turns = 1
    assert not cs._reply_hold_eligible(st)          # 인사 뒤 · 목소리 없음
    st.hold_voice_ts = 1.0
    assert cs._reply_hold_eligible(st)


@pytest.mark.asyncio
async def test_max_waits_for_late_quiet_then_hard_cap(hold_on, monkeypatch):
    """PM-DEC-1007 — 상한(1.2초)에 닿아도 학습자가 1.0초 조용해질 때까지 더 기다린다 · 계속 말하면 절대 상한에서 해제."""
    monkeypatch.setattr(cs, "REPLY_HOLD_LATE_QUIET_S", 0.6)
    monkeypatch.setattr(cs, "REPLY_HOLD_HARD_MAX_S", 2.0)
    st = _state()
    script = [(0, _voice(st)), (0, LiveEvent(kind="audio", audio=b"\x01\x00" * 10)), (0, LiveEvent(kind="turn_end"))]
    script += [(0.1, _voice(st)) for _ in range(30)]          # 학습자가 3초 내내 말한다
    ws = await _run(st, script, tail_s=0.5)
    t = ws.log[0][0]
    assert 1.95 <= t <= 2.4, t                                 # 1.2초 상한이 아니라 2.0초 절대 상한에서


@pytest.mark.asyncio
async def test_max_releases_once_late_quiet_reached(hold_on, monkeypatch):
    monkeypatch.setattr(cs, "REPLY_HOLD_LATE_QUIET_S", 0.6)
    monkeypatch.setattr(cs, "REPLY_HOLD_HARD_MAX_S", 5.0)
    st = _state()
    script = [(0, _voice(st)), (0, LiveEvent(kind="audio", audio=b"\x01\x00" * 10)), (0, LiveEvent(kind="turn_end"))]
    script += [(0.1, _voice(st)) for _ in range(10)]          # 1초 말하고 멈춘다
    ws = await _run(st, script, tail_s=1.5)
    t = ws.log[0][0]
    assert 1.5 <= t <= 2.0, t                                  # 상한(1.2) 지나 마지막 목소리(≈1.0) + 0.6 조용


@pytest.mark.asyncio
async def test_unprompted_rule(monkeypatch):
    """PM-DEC-1015 — 직전 비버 턴 뒤 학습자 목소리·전사·서버 턴이 없으면 «안 물었는데 답»."""
    st = cs._CallState()
    assert cs._reply_unprompted(st) is False                 # 비버 턴 끝 전(첫 인사) — 판정 안 함
    st.beaver_end_ts = 100.0
    monkeypatch.setattr(cs, "AGATE", True)
    assert cs._reply_unprompted(st) is True                  # 아무것도 없음(TV 조각에 답)
    st.ag_onsets = 1
    assert cs._reply_unprompted(st) is False                 # 게이트가 학습자 말을 인정
    st.ag_onsets = 0
    st.user_turn_open = True
    assert cs._reply_unprompted(st) is False                 # 학습자 전사 있음
    st.user_turn_open = False
    st.server_turn_ts = 101.0
    assert cs._reply_unprompted(st) is False                 # 서버가 연 턴(넛지·안내)의 답
    monkeypatch.setattr(cs, "AGATE", False)
    st.server_turn_ts = 0.0
    st.user_voice_ts = 101.0
    assert cs._reply_unprompted(st) is False                 # 게이트 없을 땐 0.02 목소리 프레임


@pytest.mark.asyncio
async def test_unprompted_answer_dropped_then_real_answer_sent(hold_on, monkeypatch):
    monkeypatch.setattr(cs, "REPLY_HOLD_DROP_UNPROMPTED", True)
    monkeypatch.setattr(cs, "AGATE", True)
    st = _state()
    st.beaver_end_ts = asyncio.get_running_loop().time() - 5.0
    st.hold_voice_ts = st.beaver_end_ts - 1.0

    def _learner_speaks():
        st.ag_onsets += 1
        st.hold_voice_ts = asyncio.get_running_loop().time()

    ws = await _run(st, [
        (0, LiveEvent(kind="audio", audio=b"\xAA\x00" * 10)),   # TV 조각에 지어낸 답
        (0, LiveEvent(kind="turn_end")),
        (0.3, _learner_speaks),
        (0, LiveEvent(kind="audio", audio=b"\xBB\x00" * 10)),   # 학습자 말 뒤 진짜 답
        (0, LiveEvent(kind="turn_end")),
    ])
    assert ws.audio() == [b"\xBB\x00" * 10]
    assert st.hold_dropped == 1


@pytest.mark.asyncio
async def test_unprompted_not_when_mic_heard_voice_without_gate_onset(monkeypatch):
    """PM-DEC-1018 — 게이트가 말로 인정 못 하고 전사도 없어도, 비버 턴 뒤 마이크 소리 합이 0.5초 넘으면 버리지 않는다(2632)."""
    monkeypatch.setattr(cs, "AGATE", True)
    st = cs._CallState()
    st.beaver_end_ts = 100.0
    st.voiced_since_end = 0.3
    assert cs._reply_unprompted(st) is True                  # TV 조각 정도
    st.voiced_since_end = 0.8
    assert cs._reply_unprompted(st) is False                 # 학습자가 작게라도 말함
    monkeypatch.setattr(cs, "UNPROMPTED_VOICED_S", 0.0)
    assert cs._reply_unprompted(st) is True                  # 끄면 종전 규칙


@pytest.mark.asyncio
async def test_noise_floor_drops_at_once_when_tv_switches_off(monkeypatch):
    """PM-DEC-1018 — TV 대책이 꺼져 있으면 소음 바닥은 3차처럼 작은 소리에 즉시 내려간다(켜지면 5%씩)."""
    monkeypatch.setattr(cs, "AGATE", False)
    monkeypatch.setattr(cs, "NOISE_GATE_RMS", 0.0)
    loud = int(0.05 * 32767).to_bytes(2, "little", signed=True) * 320
    soft = int(0.002 * 32767).to_bytes(2, "little", signed=True) * 320
    st = cs._CallState()
    cs._reply_hold_track_voice(st, loud)
    cs._reply_hold_track_voice(st, soft)
    assert st.hold_noise_floor == pytest.approx(0.002, abs=1e-4)
    monkeypatch.setattr(cs, "AGATE", True)
    st = cs._CallState()
    cs._reply_hold_track_voice(st, loud)
    cs._reply_hold_track_voice(st, soft)
    assert st.hold_noise_floor > 0.04                      # 켜지면 서서히
