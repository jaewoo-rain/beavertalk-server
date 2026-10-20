"""PM-DEC-872 — 표현학습 클립 주도 대리 세션 결정적 시험(외부 의존 0 · 판정·예외 응답은 가짜)."""
from __future__ import annotations

import array
import asyncio
import contextlib
import random
from types import SimpleNamespace

import pytest

from domains.learning.realtime import clip_expr
from domains.learning.realtime.clip_expr import ClipExprSession
from test_normalcall_ws import seeded, session_factory  # noqa: F401 - 픽스처

VOICED = array.array("h", [8000, -8000] * 320).tobytes()     # 40ms
SILENT = bytes(1280)
SETTINGS = SimpleNamespace(CLIP_EXPR="Baba", LIVE_REGROUND_VOICE_RMS=0.02, CLIP_EXPR_END_SILENCE_S=0.12,
                           CLIP_EXPR_DUNNO_S=0.4, CLIP_EXPR_JUDGE_MODEL="x")


@pytest.fixture(autouse=True)
def _instant_playback(monkeypatch):
    """시험에서는 클립 재생 시간을 0 으로 본다(무응답 시계가 바로 돈다). 재생 관문 시험은 따로 되돌린다."""
    monkeypatch.setattr(clip_expr, "_OUT_BPS", 10 ** 12)
    clip_expr._PROGRESS.clear()


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


def test_rule_judge():
    rj = clip_expr.rule_judge
    assert rj("안녕하세요", ["안녕하세요?"], blank=False)[0] == "correct"
    assert rj("안녕하세여", ["안녕하세요?"], blank=False)[0] == "correct"
    assert rj("", ["안녕하세요?"], blank=False)[0] == "dunno"
    assert rj("몰라요", ["안녕하세요?"], blank=False)[0] == "dunno"
    assert rj("I don't know", ["안녕하세요?"], blank=False)[0] == "dunno"
    assert rj("Hello there", ["안녕하세요?"], blank=False)[0] == "native_lang"
    assert rj("저는 민수예요", ["저는 ◯◯이에요"], blank=True)[0] == "correct"
    assert rj("고마워요", ["안녕하세요?"], blank=False)[0] is None        # 규칙이 못 정함 → LLM


class _Harness:
    def __init__(self, results: list[str], say=None, call_id=1):
        self.results = list(results)
        self.notes: list[dict] = []
        self.closed = False
        self.calls = 0
        state = SimpleNamespace(call_id=call_id, close_seed="CLOSE", call_start_ts=None, fragment_index=1)

        async def judge(pcm, ctx):
            self.calls += 1
            r = self.results.pop(0)
            return ("말" if r != "dunno" else ""), r

        async def notify(msg):
            self.notes.append(msg)

        def req_close():
            self.closed = True

        self.s = ClipExprSession(None, None, SETTINGS, {"system_instruction": "S", "voice": "Fenrir"},
                                 char="Baba", state=state, notify=notify, judge=judge, request_close=req_close,
                                 rng=random.Random(0), say=say)
        self.it = None

    async def turn(self) -> list[str]:
        """turn_end 까지 받은 out_tr 를 문장 목록으로."""
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
        assert h.notes[n]["answered"] == "1/ask"
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
        assert all(m["answered"] == "1/ask" for m in h.notes[n:])          # 답한 항목이 클립마다 정확히 찍힌다
        n = len(h.notes)
        await h.say(); await h.it.__anext__(); await h.turn()
        assert h.ids_since(n)[0].startswith("g_rep_again_") and h.ids_since(n)[1] == "rep_1"
        n = len(h.notes)
        await h.say(); await h.it.__anext__(); await h.turn()
        ids = h.ids_since(n)
        assert ids[0].startswith("g_move_") and ids[1:] == ["model_1", "ask_2_2"]   # 정답은 지금 항목(1)의 것


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
async def test_dunno_clock_waits_for_estimated_playback_end(monkeypatch):
    """call 2035 — 클립을 실시간보다 빨리 보내도, 앱이 다 틀었을 시각 전에는 «모름» 을 세지 않는다."""
    monkeypatch.setattr(clip_expr, "_OUT_BPS", 48000)
    h = _Harness([])
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        assert h.s._play_until > clip_expr.time.monotonic() + 1.0          # 인사+출제 몇 초 분량
        for _ in range(30):                                                 # 1.2초 무음 — 아직 재생 중
            await h.s.send_audio(SILENT)
        await asyncio.sleep(0.02)
        assert h.s._cmd.empty()


@pytest.mark.asyncio
async def test_other_and_english_answer_use_clips_not_tts():
    """call 2043 — 딴말·영어 답은 TTS 응답(2.5~3.9초) 대신 g_back + 지금 문제 클립으로 바로 받는다."""
    said = []

    async def say(guide, heard):
        said.append(guide)
        return "x", b"\x01\x00" * 480

    h = _Harness(["other", "native_lang"], say=say)
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        for _ in range(2):
            n = len(h.notes)
            await h.say(); await h.it.__anext__(); await h.turn()
            ids = h.ids_since(n)
            assert ids[0].startswith("g_back_") and ids[1] == "ask_1_1"
        assert not said and h.s.exc_used == 0


@pytest.mark.asyncio
async def test_explain_after_three_fails_says_then_plays_model_and_falls_back_on_failure():
    said = []

    async def say(guide, heard):
        said.append(guide)
        return "Easy one: it's a greeting.", b"\x01\x00" * 480

    h = _Harness(["wrong", "wrong", "wrong"], say=say)
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        for _ in range(2):
            await h.say(); await h.it.__anext__(); await h.turn()
        n = len(h.notes)
        await h.say(); await h.it.__anext__()
        texts = await h.turn()
        assert texts[0] == "Easy one: it's a greeting." and h.ids_since(n) == ["model_1", "ask_2_2"]
        assert len(said) == 1 and h.s.exc_used == 1


@pytest.mark.asyncio
async def test_say_failure_falls_back_to_clip():
    async def say(guide, heard):
        raise RuntimeError("tts down")

    h = _Harness(["wrong", "wrong", "wrong"], say=say)
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        for _ in range(2):
            await h.say(); await h.it.__anext__(); await h.turn()
        n = len(h.notes)
        await h.say(); await h.it.__anext__(); await h.turn()          # 3회 실패 → 설명 실패 → g_move 로 대신
        ids = h.ids_since(n)
        assert ids[0].startswith("g_move_") and ids[1:] == ["model_1", "ask_2_2"]


@pytest.mark.asyncio
async def test_speculative_judge_is_used_once_and_dropped_when_speech_resumes():
    h = _Harness(["correct", "correct"])
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        for _ in range(8):
            await h.s.send_audio(VOICED)
        for _ in range(2):                       # 0.08초 무음 — 미리 판정 문턱(0.35) 전
            await h.s.send_audio(SILENT)
        assert h.s._spec is None
        h.s._end_s = 2.0                          # 말 끝을 늦춰 미리 판정만 시작되게
        for _ in range(8):                       # 0.32초 더 → 0.40초 → 미리 판정 시작
            await h.s.send_audio(SILENT)
        assert h.s._spec is not None
        await asyncio.sleep(0)                   # 미리 판정이 한 번 돈다(가짜 판정기 호출)
        await h.s.send_audio(VOICED)             # 말이 이어졌다 → 버림
        assert h.s._spec is None
        h.s._end_s = 0.5                          # 실제(0.8)처럼 말 끝 > 미리 판정 문턱
        for _ in range(13):                      # 0.52초 — 0.36초에 미리 판정, 0.52초에 말 끝
            await h.s.send_audio(SILENT)
        await h.it.__anext__()
        await h.turn()
        assert h.s.spec_hits == 1                 # 마지막 말 끝의 미리 판정을 그대로 썼다


@pytest.mark.asyncio
async def test_fragment_resume_keeps_item_and_skips_greeting():
    h = _Harness(["correct", "correct"], call_id=77)
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        await h.say(); await h.it.__anext__(); await h.turn()
        await h.say(); await h.it.__anext__(); await h.turn()      # 항목 2 출제까지
    h2 = _Harness([], call_id=77)
    async with h2.s:
        h2.it = h2.s.events().__aiter__()
        await h2.s.send_text_turn("RESUME")
        await h2.turn()
        assert h2.ids_since(0) == ["ask_2_2"] and h2.s.idx == 1


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

    async def judge(self, pcm, targets, blank, st):
        return "안녕하세요", "correct"

    monkeypatch.setattr(ClipExprSession, "_analyze_real", judge)
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
    assert any(r.getMessage().startswith("normalcall clipexpr 원가: call_id=") and "clip_usd=" in r.getMessage()
               and "judge_model=" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_progress_restored_from_remote_on_other_instance(monkeypatch):
    """PM-DEC-915 — 이어하기 조각이 다른 인스턴스로 가면(메모리에 없음) GCS 진행 파일로 이어 간다."""
    from types import SimpleNamespace as NS
    store = {}
    monkeypatch.setattr(clip_expr, "_progress_store", lambda cid, snap: store.__setitem__(cid, snap))
    monkeypatch.setattr(clip_expr, "_progress_load", lambda cid: store.get(cid))
    st = NS(call_id=77, close_seed="CLOSE", call_start_ts=None, fragment_index=1)
    s1 = clip_expr.ClipExprSession(None, None, NS(CLIP_EXPR="Baba", LIVE_REGROUND_VOICE_RMS=0.02), {"system_instruction": "S"},
                                   char="Baba", state=st, notify=None, request_close=None)
    async with s1:
        s1.idx = 3
        s1._save()
    assert store[77]["idx"] == 3
    clip_expr._PROGRESS.clear()                        # 다른 인스턴스 = 메모리 없음
    st2 = NS(call_id=77, close_seed="CLOSE", call_start_ts=None, fragment_index=2)
    s2 = clip_expr.ClipExprSession(None, None, NS(CLIP_EXPR="Baba", LIVE_REGROUND_VOICE_RMS=0.02), {"system_instruction": "S"},
                                   char="Baba", state=st2, notify=None, request_close=None)
    async with s2:
        assert s2._restored and s2.idx == 3
