"""PM-DEC-864 — 턴마다 새 Live 세션(CONTEXT_MODE=perturn) 결정적 시험(외부 의존 0)."""
from __future__ import annotations

import array
import asyncio
import contextlib
import logging

import pytest

import domains.learning.realtime.call_session as cs
from core.config import settings as app_settings
from core.gemini_live import LiveEvent
from domains.learning.realtime.call_session import run_call
from domains.learning.realtime.perturn_session import PerTurnSession
from test_normalcall_ws import FakeWebSocket, _mock_external, _start_incoming, _wait_analysis_tasks, seeded, session_factory  # noqa: F401

VOICED = array.array("h", [8000, -8000] * 320).tobytes()     # RMS ≫ 재접지 문턱
SILENT = bytes(1280)


class _Sess:
    """스크립트 세션 — 학습자 음성이 오면 한 턴 답하고, 마지막 세션이면 스트림을 닫는다."""

    def __init__(self, n: int, system_instruction: str, *, greet: bool, last_after: int | None):
        self.n, self.si, self.greet, self.last_after = n, system_instruction, greet, last_after
        self.audio: list[bytes] = []
        self.texts: list[str] = []
        self.regrounds: list[tuple[str, bool]] = []
        self.voiced = asyncio.Event()
        self.closed = False

    async def send_audio(self, pcm):
        self.audio.append(pcm)
        if pcm == VOICED:
            self.voiced.set()

    async def send_text_turn(self, text):
        self.texts.append(text)

    async def send_reground(self, text, *, turn_complete=True):
        self.regrounds.append((text, turn_complete))

    async def send_persona(self, text):
        self.texts.append(text)

    async def send_tool_response(self, *a, **k):
        return None

    async def events(self):
        if self.greet:
            yield LiveEvent(kind="out_tr", text="안녕! 나는 바바야.")
            yield LiveEvent(kind="audio", audio=b"\x01\x00" * 480)
            yield LiveEvent(kind="usage", usage=type("U", (), {"prompt_token_count": 900, "response_token_count": 50})())
            yield LiveEvent(kind="turn_end")
        await self.voiced.wait()
        yield LiveEvent(kind="in_tr", text="고양이 이름은 나비예요", is_final=True)
        yield LiveEvent(kind="out_tr", text=f"나비 귀엽다! ({self.n})")
        yield LiveEvent(kind="audio", audio=b"\x02\x00" * 480)
        yield LiveEvent(kind="turn_end")
        if self.last_after is not None:
            return
        await asyncio.Event().wait()


def _factory(made: list, *, last_n: int):
    @contextlib.asynccontextmanager
    async def factory(client, settings, **kw):
        n = len(made) + 1
        s = _Sess(n, kw["system_instruction"], greet=(n == 1), last_after=(0 if n >= last_n else None))
        made.append(s)
        try:
            yield s
        finally:
            s.closed = True
    return factory


@pytest.mark.asyncio
async def test_perturn_swaps_session_on_mic_and_carries_context(session_factory, seeded, monkeypatch, caplog):
    """인사 턴 뒤 다음 세션을 미리 열고, 마이크가 열린 첫 프레임에서 바꾼다 — 학습자 음성은 새 세션으로만 간다."""
    monkeypatch.setattr(app_settings, "CONTEXT_MODE", "perturn")
    monkeypatch.setattr(app_settings, "LIVE_USAGE_RESP_LOG", True)
    caplog.set_level(logging.INFO)
    made: list[_Sess] = []

    async def wait_second_ready():
        for _ in range(400):
            if len(made) >= 2:
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.02)

    incoming = [_start_incoming(seeded), wait_second_ready,
                {"type": "websocket.receive", "bytes": SILENT},
                {"type": "websocket.receive", "bytes": VOICED}]
    ws = FakeWebSocket(incoming, hang=True)
    await run_call(ws, app_settings, object(), session_factory, member_id=seeded["member_id"],
                   live_session_factory=_factory(made, last_n=2))
    await _wait_analysis_tasks()

    # 두 번째 턴 끝에서 세 번째 세션을 미리 열었다가 통화가 끝났다 — 쓰이지 않았다
    assert len(made) in (2, 3), [s.n for s in made]
    first, second = made[:2]
    assert all(not s.audio for s in made[2:])
    assert first.texts, "1세대 선톡 시드"
    assert VOICED not in first.audio, "교체 뒤 학습자 음성이 옛 세션으로 갔다"
    assert VOICED in second.audio
    assert "[지금까지의 통화]" in second.si and "너(비버): 안녕! 나는 바바야." in second.si and "직전 네 끝맺음: «나는 바바야.»" in second.si
    assert "[지금까지의 통화]" not in first.si
    assert not second.texts, "새 세션에는 시드를 다시 보내지 않는다(학습자 음성으로 연다)"
    assert first.closed
    assert any(r.getMessage().startswith("normalcall resp: call_id=") and "prompt=900" in r.getMessage() and "gen=1" in r.getMessage()
               for r in caplog.records)
    assert any("normalcall perturn: call_id=" in r.getMessage() and "swaps=1" in r.getMessage() and "max_open=2" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_perturn_off_by_default_keeps_single_session(session_factory, seeded, monkeypatch):
    monkeypatch.setattr(app_settings, "CONTEXT_MODE", "")
    made: list[_Sess] = []

    async def pause():
        await asyncio.sleep(0.05)

    ws = FakeWebSocket([_start_incoming(seeded), pause, {"type": "websocket.receive", "bytes": VOICED}], hang=True)
    await run_call(ws, app_settings, object(), session_factory, member_id=seeded["member_id"],
                   live_session_factory=_factory(made, last_n=1))
    await _wait_analysis_tasks()
    assert len(made) == 1 and "[지금까지의 통화]" not in made[0].si


# ── 대리 세션 단위 ─────────────────────────────────────────────────────────── #
class _Unit:
    def __init__(self, n):
        self.n = n
        self.audio = []
        self.texts = []
        self.q: asyncio.Queue = asyncio.Queue()

    async def send_audio(self, pcm):
        self.audio.append(pcm)

    async def send_text_turn(self, t):
        self.texts.append(t)

    async def send_reground(self, t, *, turn_complete=True):
        self.texts.append(t)

    async def send_persona(self, t):
        self.texts.append(t)

    async def send_tool_response(self, *a, **k):
        return None

    async def events(self):
        while True:
            ev = await self.q.get()
            if ev is None:
                return
            yield ev


def _unit_factory(made, fail_at=None, gate=None):
    @contextlib.asynccontextmanager
    async def f(client, settings, **kw):
        n = len(made) + 1
        if gate is not None and n > 1:
            await gate.wait()
        if fail_at == n:
            made.append(None)
            raise RuntimeError("connect fail")
        s = _Unit(n)
        made.append(s)
        yield s
    return f


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_unit_swap_routes_and_old_usage_still_counts():
    made = []
    p = PerTurnSession(_unit_factory(made), None, None, {"system_instruction": "S", "voice": "Fenrir", "resume_handle": "h"})
    async with p:
        it = p.events().__aiter__()
        made[0].q.put_nowait(LiveEvent(kind="turn_end"))
        assert (await it.__anext__()).kind == "turn_end"
        p.prepare("S2")
        await _settle()
        assert p.has_next and len(made) == 2
        assert p.on_mic_frame(False) is True
        await p.send_audio(b"x")
        assert made[1].audio == [b"x"] and made[0].audio == []
        made[0].q.put_nowait(LiveEvent(kind="usage"))       # 물러난 세션의 늦은 usage — 흘린다
        made[0].q.put_nowait(LiveEvent(kind="out_tr", text="옛"))  # 나머지는 버린다
        made[1].q.put_nowait(LiveEvent(kind="out_tr", text="새"))
        kinds = [(await it.__anext__()).kind for _ in range(2)]
        assert sorted(kinds) == ["out_tr", "usage"]
        assert p.stats()["swaps"] == 1 and p.stats()["max_open"] <= 2


@pytest.mark.asyncio
async def test_unit_control_send_and_voice_before_ready_keep_current():
    made = []
    gate = asyncio.Event()
    p = PerTurnSession(_unit_factory(made, gate=gate), None, None, {"system_instruction": "S"})
    async with p:
        p.prepare("S2")
        await _settle()
        assert p.on_mic_frame(True) is False and not p.has_next      # 아직 연결 중 + 학습자 말 시작 → 지금 세션 유지
        assert p.stats()["skipped"] == {"not_ready": 1}
        gate.set()
        p.prepare("S3")
        await _settle()
        assert p.has_next
        await p.send_text_turn("넛지")                               # 준비 뒤 제어 텍스트 → 준비한 세션은 그 응답을 모른다
        assert p.on_mic_frame(False) is False and not p.has_next
        assert p.stats()["skipped"].get("control") == 1
        await p.send_audio(b"y")
        assert made[0].audio == [b"y"]


@pytest.mark.asyncio
async def test_unit_open_failure_keeps_current():
    made = []
    p = PerTurnSession(_unit_factory(made, fail_at=2), None, None, {"system_instruction": "S"})
    async with p:
        p.prepare("S2")
        await _settle()
        assert p.on_mic_frame(False) is False
        assert p.stats()["open_failures"] == 1
        await p.send_audio(b"z")
        assert made[0].audio == [b"z"]


def test_compression_observer_off_in_perturn():
    st = cs._CallState()
    st.ctx = object()
    cs._observe_compression(st, 9000)
    cs._observe_compression(st, 1000)
    assert st.compression_seen == 0 and st.usage_prompt_max == 9000


@pytest.mark.asyncio
async def test_perturn_mic_seat_text_not_sent_and_note_in_instruction():
    """PM-DEC-873 — perturn 이면 매 턴 안내를 마이크 자리 텍스트로 보내지 않고 다음 세션 지시문에 넣는다."""
    st = cs._CallState()
    st.ctx = object()
    st.ctx_base = "BASE"
    st.turn_note = cs.TURN_NOTE_FREETALK
    st.beaver_turns = 3
    sent = []

    class S:
        async def send_reground(self, text, *, turn_complete=True):
            sent.append(text)

    await cs._attach_reground(S(), st, "마이크")
    assert sent == [] and not st.turn_note_sent
    si = cs._perturn_instruction(st)
    assert "이번 턴 답하는 법: 짧게 반응하고" in si and cs.CONTROL_TAG not in si and "읽지 말고" not in si
    st.ctx = None                                   # 종전 경로는 그대로 얹는다
    await cs._attach_reground(S(), st, "마이크")
    assert sent == [cs.TURN_NOTE_FREETALK]


def test_after_turn_end_skip_reasons():
    st = cs._CallState()

    class P:
        ctrl_sends = 0
        skips: list = []
        prepared: list = []

        def _skip(self, why):
            self.skips.append(why)

        def prepare(self, si):
            self.prepared.append(si)

    st.ctx = P()
    st.ctx_base = "BASE"
    st.should_close = True
    cs._ctx_after_turn_end(st, 0, "안녕", False)
    st.should_close = False
    cs._ctx_after_turn_end(st, 0, "안녕", True)
    st.ctx.ctrl_sends = 1
    cs._ctx_after_turn_end(st, 0, "안녕", False)
    st.ctx.ctrl_sends = 0
    st.ctx_turn_interrupted = True
    cs._ctx_after_turn_end(st, 0, "안녕", False)
    st.ctx_turn_interrupted = False
    st.segments = [{"role": "user", "text": "나비예요"}, {"role": "beaver", "text": "귀엽다"}]
    st.face_last = "happy"
    st.ctx_summary = "고양이 나비"
    cs._ctx_after_turn_end(st, 0, "귀엽다", False)
    assert P.skips == ["closing", "reseed", "control", "interrupted"]
    si = P.prepared[-1]
    assert si.startswith("BASE") and "줄거리: 고양이 나비" in si and "학습자: 나비예요" in si and "지금 네 표정: happy" in si


@pytest.mark.asyncio
async def test_unit_reopen_swaps_immediately_and_keeps_current_on_failure():
    """PM-DEC-884 — 벙어리 인사: 같은 세션에 다시 시드하지 않고 새 세션을 열어 거기로 보낸다."""
    made = []
    p = PerTurnSession(_unit_factory(made, fail_at=3), None, None, {"system_instruction": "S"})
    async with p:
        assert await p.reopen("S", why="mute_greeting") is True
        await p.send_text_turn("시드")
        assert made[1].texts == ["시드"] and made[0].texts == []
        assert await p.reopen("S", why="mute_greeting") is False      # 세 번째 연결 실패 → 지금(2번) 유지
        await p.send_audio(b"a")
        assert made[1].audio == [b"a"] and p.stats()["reopens"] == 1 and p.stats()["open_failures"] == 1


def test_summary_model_default_is_not_the_retired_lite():
    assert app_settings.CONTEXT_SUMMARY_MODEL != "gemini-2.5-flash-lite"


@pytest.mark.asyncio
async def test_perturn_mute_greeting_reopens_new_session_and_reseeds_there(session_factory, seeded, monkeypatch):
    """call 2028 — 첫 세션 인사가 전사만(0B)이면 새 세션을 열어 시드를 거기로 보낸다(같은 세션에 다시 보내지 않는다)."""
    monkeypatch.setattr(app_settings, "CONTEXT_MODE", "perturn")
    made: list = []

    class _Mute(_Sess):
        async def events(self):
            if self.n == 1:
                yield LiveEvent(kind="out_tr", text="안녕! 나는 바바야.")
                yield LiveEvent(kind="turn_end")
                await asyncio.Event().wait()
            await self.got_seed.wait()
            yield LiveEvent(kind="out_tr", text="안녕! 나는 바바야.")
            yield LiveEvent(kind="audio", audio=b"\x01\x00" * 480)
            yield LiveEvent(kind="turn_end")

        async def send_text_turn(self, text):
            self.texts.append(text)
            self.got_seed.set()

    @contextlib.asynccontextmanager
    async def factory(client, settings, **kw):
        s = _Mute(len(made) + 1, kw["system_instruction"], greet=False, last_after=None)
        s.got_seed = asyncio.Event()
        made.append(s)
        yield s

    async def wait_greeted():
        for _ in range(400):
            if len(made) >= 2 and made[1].texts:
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)

    ws = FakeWebSocket([_start_incoming(seeded), wait_greeted], hang=False)
    await run_call(ws, app_settings, object(), session_factory, member_id=seeded["member_id"], live_session_factory=factory)
    await _wait_analysis_tasks()
    assert len(made) >= 2
    assert len(made[0].texts) == 1, "벙어리 세션에 시드를 다시 보냈다"
    assert made[1].texts, "새 세션에 재시드가 안 갔다"
