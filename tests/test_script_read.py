"""PM-DEC-877 — 대본 읽기 대리 세션 결정적 시험(외부 의존 0 · STT 는 가짜)."""
from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

import pytest

from domains.learning.realtime import script_read as sr
from domains.learning.realtime.script_read import ScriptReadSession
from test_clip_expr import SILENT, VOICED, _instant_playback  # noqa: F401 - 자동 픽스처

SETTINGS = SimpleNamespace(SCRIPT_READ="Baba", LIVE_REGROUND_VOICE_RMS=0.02, CLIP_EXPR_END_SILENCE_S=0.12, SCRIPT_READ_END_SILENCE_S=0.12,
                           CLIP_EXPR_DUNNO_S=0.4)


def test_match_is_loose_and_never_angry_on_exact_read():
    line = "조금 싱겁네요. 그래도 맛있어요."
    assert sr.match(line, "조금 싱겁네요 그래도 맛있어요")[0] == "pass"
    assert sr.match(line, "조금 시겁네요 그래도 마시써요")[0] == "pass"          # STT 오인식 정도는 통과
    assert sr.match(line, "")[0] == "empty"
    assert sr.match(line, "It's a little bland")[0] == "fail"                      # 다른 언어
    assert sr.match(line, "사이다 주세요")[0] == "fail"
    _, clips_items, lines = None, *sr.load_bank("Baba")[1:]
    for x in lines.values():                                                        # 대본 줄 그대로 읽으면 늘 통과
        assert sr.match(x["learner"], x["learner"])[0] == "pass"


def test_enabled_conditions():
    on = SimpleNamespace(SCRIPT_READ="Baba")
    assert sr.enabled(on, call_type="chat", voice="Fenrir", locale="en") == "Baba"
    assert sr.enabled(on, call_type="freetalk", voice="Fenrir", locale="en") == "Baba"
    assert sr.enabled(on, call_type="homework", voice="Fenrir", locale="en") is None
    assert sr.enabled(on, call_type="chat", voice="Leda", locale="en") is None
    assert sr.enabled(SimpleNamespace(SCRIPT_READ=""), call_type="chat", voice="Fenrir", locale="en") is None


class _H:
    def __init__(self, heard: list[str]):
        self.heard = list(heard)
        self.notes: list[dict] = []
        self.closed = False
        state = SimpleNamespace(call_id=1, close_seed="CLOSE", call_start_ts=None, fragment_index=1)

        async def stt(pcm, hint):
            return self.heard.pop(0)

        async def notify(m):
            self.notes.append(m)

        def req_close():
            self.closed = True

        self.s = ScriptReadSession(None, None, SETTINGS, {"system_instruction": "S", "voice": "Fenrir"}, char="Baba",
                                   state=state, notify=notify, request_close=req_close, rng=random.Random(1), stt=stt)
        self.it = None

    async def turn(self):
        while True:
            ev = await asyncio.wait_for(self.it.__anext__(), 2)
            if ev.kind == "turn_end":
                await asyncio.sleep(0.01)   # PM-DEC-893 — 재생 시작 시각에 맞춘 띠 알림(태스크)을 흘려 보낸다
                return

    async def say(self):
        for _ in range(8):
            await self.s.send_audio(VOICED)
        for _ in range(4):
            await self.s.send_audio(SILENT)
        await self.it.__anext__()          # in_tr
        await self.turn()

    def clips(self, n=0):
        return [m["id"] for m in self.notes[n:] if m["type"] == "clip"]

    def cards(self, kind, n=0):
        return [m for m in self.notes[n:] if m["type"] == kind]


@pytest.mark.asyncio
async def test_flow_pass_fail_twice_skip_and_angry_once_per_line():
    lines = sr.load_bank("Baba")[2]
    h = _H([lines[1]["learner"], "사이다 주세요", "모르겠어요 음", "", "케이크"])
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        assert h.clips()[0].startswith("sg_open_") and h.clips()[1:] == ["sb_1", "sp_1"]
        assert h.cards("script_line")[0]["n"] == 1 and h.cards("script_line")[0]["total"] == 11
        # PM-DEC-893 — 띠 순서: 시작 클립(script_say open) → 바바 줄(script_beaver) → 학습자 줄(script_line)
        assert [m["type"] for m in h.notes if m["type"].startswith("script_")] == ["script_say", "script_beaver", "script_line"]
        assert h.cards("script_say")[0]["kind"] == "open" and h.cards("script_say")[0]["text"]
        b = h.cards("script_beaver")[0]
        assert b["n"] == 1 and b["ko"] == lines[1]["beaver"] and b["gloss"] and b["roman"]
        n = len(h.notes)
        await h.say()                                   # 1번 정확히 → 잘함 + 2번
        assert h.clips(n)[0].startswith("sg_good_") and h.clips(n)[1:] == ["sb_2", "sp_2"]
        assert h.cards("script_result", n)[0]["result"] == "pass"
        n = len(h.notes)
        await h.say()                                   # 2번 틀림 1회 → 화내기
        assert h.clips(n)[0].startswith("sg_angry_") and h.cards("script_line", n)[0]["attempt"] == 2
        assert h.cards("script_say", n)[0]["kind"] == "angry"
        band = [m["type"] for m in h.notes[n:] if m["type"] in ("script_say", "script_line")]
        assert band == ["script_say", "script_line"]   # 화냄 클립 뒤에 같은 줄(다시 읽기)
        n = len(h.notes)
        await h.say()                                   # 2번 틀림 2회 → 정답 들려주고 넘어감
        assert h.clips(n) == ["sm_2", "sb_3", "sp_3"] and h.cards("script_result", n)[0]["result"] == "skip"
        assert h.cards("script_say", n)[0]["kind"] == "answer" and h.cards("script_beaver", n)[0]["n"] == 3
        n = len(h.notes)
        await h.say()                                   # 빈 STT → 다시(화내지 않음)
        assert h.clips(n)[0].startswith("sg_again_") and not h.cards("script_result", n)
        n = len(h.notes)
        await h.say()                                   # 3번 틀림 1회 → 화내기
        assert h.clips(n)[0].startswith("sg_angry_")


@pytest.mark.asyncio
async def test_all_eleven_lines_end_with_script_end_and_close_request():
    lines = sr.load_bank("Baba")[2]
    h = _H([lines[i]["learner"] for i in range(1, 12)])
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        for _ in range(11):
            await h.say()
        assert h.closed and h.cards("script_end")
        n = len(h.notes)
        await h.s.send_text_turn("CLOSE")
        await h.turn()
        assert h.clips(n)[0].startswith("sg_close_call_")
        seen = h.clips()
        assert all(a != b for a, b in zip(seen, seen[1:]))


@pytest.mark.asyncio
async def test_finished_script_resume_closes_without_again_loop():
    """call 2040 조각2 — 11줄을 끝낸 통화에 이어하기로 붙으면 «Again» 반복 없이 바로 마무리한다."""
    lines = sr.load_bank("Baba")[2]
    h = _H([lines[i]["learner"] for i in range(1, 12)])
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        for _ in range(11):
            await h.say()
    h2 = _H([])
    async with h2.s:
        h2.it = h2.s.events().__aiter__()
        await h2.s.send_text_turn("RESUME")
        await asyncio.sleep(0.05)
        assert h2.closed and not h2.clips()
        assert h2.s._state.call_start_ts is not None   # PM-DEC-892 — 시계가 서야 종료 워처가 작별 시드를 넣는다(call 2075)
        await h2.s.send_text_turn("CLOSE")
        await h2.turn()
        assert h2.clips()[0].startswith("sg_close_call_")


@pytest.mark.asyncio
async def test_speech_during_playback_is_dropped(monkeypatch):
    """call 2040·2041 — 판정 뒤 비버 소리가 나오는 동안 들어온 말(줄의 뒤 반쪽)은 다음 시도로 치지 않는다."""
    from domains.learning.realtime import clip_expr
    monkeypatch.setattr(clip_expr, "_OUT_BPS", 48000)
    h = _H([])
    async with h.s:
        h.it = h.s.events().__aiter__()
        await h.s.send_text_turn("SEED")
        await h.turn()
        for _ in range(8):
            await h.s.send_audio(VOICED)
        for _ in range(4):
            await h.s.send_audio(SILENT)
        await asyncio.sleep(0.02)
        assert h.s._cmd.empty() and not h.s._speech
