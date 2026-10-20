"""PM-DEC-872 — 표현학습 클립 주도 대리 세션 결정적 시험(외부 의존 0 · 판정기는 가짜)."""
from __future__ import annotations

import array
import asyncio
import contextlib
import random
from types import SimpleNamespace

import pytest

from core.gemini_live import LiveEvent
from domains.learning.realtime import clip_expr
from domains.learning.realtime.clip_expr import ClipExprSession
from test_normalcall_ws import seeded, session_factory  # noqa: F401 - 픽스처

VOICED = array.array("h", [8000, -8000] * 320).tobytes()     # 40ms
SILENT = bytes(1280)
SETTINGS = SimpleNamespace(CLIP_EXPR="Baba", LIVE_REGROUND_VOICE_RMS=0.02, CLIP_EXPR_END_SILENCE_S=0.12,
                           CLIP_EXPR_DUNNO_S=0.4, CLIP_EXPR_JUDGE_MODEL="x")


def test_enabled_only_for_baba_expression_en():
    on = SimpleNamespace(CLIP_EXPR="Baba")
    assert clip_expr.enabled(on, call_type="expression", voice="Fenrir", locale="en") == "Baba"
    assert clip_expr.enabled(on, call_type="freetalk", voice="Fenrir", locale="en") is None
    assert clip_expr.enabled(on, call_type="expression", voice="Leda", locale="en") is None
    assert clip_expr.enabled(on, call_type="expression", voice="Fenrir", locale="ja") is None
    assert clip_expr.enabled(SimpleNamespace(CLIP_EXPR=""), call_type="expression", voice="Fenrir", locale="en") is None


def test_bank_has_every_clip_the_flow_names():
    clips, items = clip_expr.load_bank("Baba")
    assert len(items) == 15 and len(clips) == 138
    for it in items:
        for k in ("ask_%d_1", "ask_%d_2", "quiz_%d", "hint_%d", "model_%d"):
            assert k % it.seq in clips
        if not it.blank:
            assert f"rep_{it.seq}" in clips and f"native_{it.seq}" in clips
    assert [it.seq for it in items if it.blank] == [6, 7]
    assert all(len(c.pcm) > 0 for c in clips.values())


class _Harness:
    def __init__(self, results: list[str], live_factory=None):
        self.results = list(results)
        self.notes: list[dict] = []
        self.closed = False
        state = SimpleNamespace(call_id=1, close_seed="CLOSE", call_start_ts=None)

        async def judge(wav, prompt):
            r = self.results.pop(0)
            return ("말" if r != "dunno" else ""), r, 10, 5

        async def notify(msg):
            self.notes.append(msg)

        def req_close():
            self.closed = True

        self.s = ClipExprSession(live_factory or _no_live, None, SETTINGS, {"system_instruction": "S", "voice": "Fenrir"},
                                 char="Baba", state=state, notify=notify, judge=judge, request_close=req_close,
                                 rng=random.Random(0))
        self.it = None

    async def turn(self) -> list[str]:
        """turn_end 까지 받은 out_tr 를 클립 문장 목록으로."""
        texts = []
        while True:
            ev = await asyncio.wait_for(self.it.__anext__(), 2)
            if ev.kind == "turn_end":
                return texts
            if ev.kind == "out_tr":
                texts.append(ev.text.strip())

    async def say(self):
        for _ in range(8):
            await self.s.send_audio(VOICED)
        for _ in range(4):
            await self.s.send_audio(SILENT)

    def ids_since(self, n: int) -> list[str]:
        return [m["id"] for m in self.notes[n:]]


@contextlib.asynccontextmanager
async def _no_live(client, settings, **kw):
    raise AssertionError("이 시험에서는 Live 를 열면 안 된다")
    yield  # pragma: no cover


@pytest.mark.asyncio
async def test_happy_path_ask_native_then_next_item():
    h = _Harness(["correct", "correct"])
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        assert h.ids_since(0)[0].startswith("g_open_") and h.ids_since(0)[1] == "ask_1_1"
        n = len(h.notes)
        await h.say()
        ev = await h.it.__anext__()
        assert ev.kind == "in_tr" and ev.is_final
        await h.turn()
        ids = h.ids_since(n)
        assert ids[0].startswith("g_ok_") and ids[1] == "native_1"
        assert [m["face"] for m in h.notes[n:]] == ["happy", "neutral"]
        n = len(h.notes)
        await h.say()
        await h.it.__anext__()
        await h.turn()
        ids = h.ids_since(n)
        assert ids[0].startswith("g_rep_ok_") and ids[1] == "ask_2_2"


@pytest.mark.asyncio
async def test_wrong_then_model_then_rep_fail_twice_moves_on_with_explain_fallback_at_cap():
    h = _Harness(["wrong", "wrong", "wrong"])
    h.s.exc_used = clip_expr.EXC_CAP          # 예외 상한 — 세 번째 실패의 «설명» 은 g_move 로 대신
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        n = len(h.notes)
        await h.say(); await h.it.__anext__(); await h.turn()
        assert h.ids_since(n)[-1] == "model_1" and any(i.startswith("g_wrong_") for i in h.ids_since(n))
        n = len(h.notes)
        await h.say(); await h.it.__anext__(); await h.turn()
        assert h.ids_since(n)[0].startswith("g_rep_again_") and h.ids_since(n)[1] == "rep_1"
        n = len(h.notes)
        await h.say(); await h.it.__anext__(); await h.turn()
        assert h.ids_since(n)[0].startswith("g_move_") and h.ids_since(n)[1] == "ask_2_2"


@pytest.mark.asyncio
async def test_silence_is_dunno_and_close_seed_plays_close():
    h = _Harness([])
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        n = len(h.notes)
        for _ in range(12):                      # 0.48초 무음 > DUNNO 0.4
            await h.s.send_audio(SILENT)
        ev = await h.it.__anext__()
        assert ev.kind == "in_tr"
        await h.turn()
        assert h.ids_since(n)[0].startswith("g_dunno_") and h.ids_since(n)[1] == "model_1"
        n = len(h.notes)
        await h.s.send_text_turn("CLOSE")
        await h.turn()
        assert h.ids_since(n)[0].startswith("g_close_")


@pytest.mark.asyncio
async def test_other_opens_one_live_turn_then_reasks():
    made = []

    class _Live:
        def __init__(self, si):
            self.si = si
            self.texts = []

        async def send_text_turn(self, t):
            self.texts.append(t)

        async def send_tool_response(self, *a, **k):
            return None

        async def events(self):
            yield LiveEvent(kind="out_tr", text="Cute question. Focus.")
            yield LiveEvent(kind="audio", audio=b"\x01\x00" * 480)
            yield LiveEvent(kind="turn_end")

    @contextlib.asynccontextmanager
    async def live(client, settings, **kw):
        s = _Live(kw["system_instruction"])
        made.append(s)
        yield s

    h = _Harness(["other"], live_factory=live)
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        n = len(h.notes)
        await h.say(); await h.it.__anext__()
        texts = await h.turn()
        assert texts[0] == "Cute question. Focus." and h.ids_since(n) == ["ask_1_1"]
        assert len(made) == 1 and "안녕하세요?" in made[0].si and h.s.exc_used == 1


@pytest.mark.asyncio
async def test_quiz_after_three_items_and_close_request_after_last():
    h = _Harness(["correct"] * 200)
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        seen = []
        for _ in range(80):
            n = len(h.notes)
            await h.say(); await h.it.__anext__(); await h.turn()
            seen += h.ids_since(n)
            if h.closed:
                break
        assert h.closed and h.s.done_items == 15
        q = seen.index("quiz_1")
        assert seen[q - 1].startswith("g_quiz_start_") and seen.index("quiz_2") > q and "quiz_15" in seen
        assert all(a != b for a, b in zip(seen, seen[1:]))      # 같은 클립 연속 0


# ── run_call 끝까지(대리 세션이 Live 자리를 맡는다) ────────────────────────── #
@pytest.mark.asyncio
async def test_run_call_with_clip_expr_never_opens_live_and_streams_clips(session_factory, seeded, monkeypatch, caplog):
    import json as _json
    import logging

    import domains.learning.realtime.call_session as cs
    from core.config import settings as app_settings
    from domains.learning.realtime.call_session import run_call
    from test_normalcall_ws import FakeWebSocket, _start_incoming, _wait_analysis_tasks

    caplog.set_level(logging.INFO)
    monkeypatch.setattr(cs._clip_expr, "enabled", lambda *a, **k: "Baba")
    monkeypatch.setattr(app_settings, "CLIP_EXPR_END_SILENCE_S", 0.12)

    async def judge(self, wav, prompt):
        return "안녕하세요", "correct", 100, 10

    monkeypatch.setattr(ClipExprSession, "_judge_llm", judge)
    opened = []

    @contextlib.asynccontextmanager
    async def live(client, settings, **kw):
        opened.append(kw)
        raise AssertionError("기본 통화에 Live 를 열었다")
        yield  # pragma: no cover

    ws_box = {}

    def clips_sent():
        return [_json.loads(t)["id"] for t in ws_box["ws"].sent_text if '"type": "clip"' in t]

    async def wait_clips(n):
        for _ in range(400):
            if len(clips_sent()) >= n:
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)

    incoming = [_start_incoming(seeded, call_type="expression"), lambda: wait_clips(2)]
    incoming += [{"type": "websocket.receive", "bytes": VOICED}] * 8 + [{"type": "websocket.receive", "bytes": SILENT}] * 6
    incoming += [lambda: wait_clips(4)]
    ws = FakeWebSocket(incoming)
    ws_box["ws"] = ws
    await run_call(ws, app_settings, object(), session_factory, member_id=seeded["member_id"], live_session_factory=live)
    await _wait_analysis_tasks()
    ids = clips_sent()
    assert not opened
    assert ids[0].startswith("g_open_") and ids[1] == "ask_1_1" and ids[2].startswith("g_ok_") and ids[3] == "native_1"
    assert ws.sent_bytes, "클립 오디오가 앱으로 가지 않았다"
    assert any("input_transcript" in t and "안녕하세요" in t for t in ws.sent_text)
    assert any(r.getMessage().startswith("normalcall clipexpr 요약: call_id=") for r in caplog.records)
