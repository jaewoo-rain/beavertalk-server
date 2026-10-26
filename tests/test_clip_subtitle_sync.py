"""클립 통화 자막 = 소리 위치 마커(2026-10-26 call 2946 「자막이 Voice랑 아예 노출되는 속도가 다른데? 싱크 맞춰」).

클립은 글과 소리를 한 번에 보낸다. output_transcript 로 보내면 앱이 도착 즉시 통째로 그려 자막이 소리보다 먼저 끝난다.
sync out_tr 은 sentence{text, server_bytes} 로 나가고(오디오 봉투 위치 · 타자기), 빈 sync 는 클립 끝 위치만 알린다.
Live out_tr(sync 없음)은 output_transcript 그대로다.
"""
from __future__ import annotations

import json

import pytest

import domains.learning.realtime.call_session as cs
from core.gemini_live import LiveEvent


class _WS:
    def __init__(self):
        self.frames: list = []

    async def send_text(self, text: str) -> None:
        self.frames.append(("text", json.loads(text)))

    async def send_bytes(self, data: bytes) -> None:
        self.frames.append(("bytes", len(data)))


async def _clip(ws, st, text: str, nbytes: int):
    """clip_expr._emit_pcm 과 같은 순서: 글(sync) → 오디오 → 빈 마커(sync)."""
    await cs._forward_event(ws, LiveEvent(kind="out_tr", text=text, sync=True), st)
    await cs._forward_event(ws, LiveEvent(kind="audio", audio=b"\x00" * nbytes), st)
    await cs._forward_event(ws, LiveEvent(kind="out_tr", text="", sync=True), st)


@pytest.mark.asyncio
async def test_clip_subtitle_rides_the_audio_as_markers_not_output_transcript():
    st = cs._CallState()
    st.learner_spoke = True
    ws = _WS()
    await _clip(ws, st, "네, 성함이 어떻게 되세요?", 9600)
    await _clip(ws, st, " 천천히 말해 주세요.", 4800)
    await cs._forward_event(ws, LiveEvent(kind="turn_end"), st)
    types = [p.get("type") if k == "text" else "bytes" for k, p in ws.frames]
    assert "output_transcript" not in types, "클립 자막은 통째로 먼저 뜨는 output_transcript 로 안 나간다"
    sub = [p for k, p in ws.frames if k == "text" and p.get("type") == "sentence" and "server_bytes" in p
           and p["server_bytes"] is not None]
    assert [(m["text"], m["server_bytes"]) for m in sub] == [
        ("네, 성함이 어떻게 되세요?", 0), ("", 9600), ("천천히 말해 주세요.", 9600), ("", 14400)]
    # 글 마커는 자기 오디오 앞, 빈 마커는 자기 오디오 뒤(앱이 차분으로 소리 길이를 안다)
    first_text = next(i for i, (k, p) in enumerate(ws.frames) if k == "text" and p.get("text") == "네, 성함이 어떻게 되세요?")
    first_bytes = types.index("bytes")
    assert first_text < first_bytes
    assert types.index("turn_start") < first_text
    # 저장·읽기 속도 재료는 그대로 받는다
    assert "".join(st.cur_beaver_text) == "네, 성함이 어떻게 되세요? 천천히 말해 주세요."


@pytest.mark.asyncio
async def test_live_transcript_is_unchanged():
    st = cs._CallState()
    st.learner_spoke = True
    ws = _WS()
    await cs._forward_event(ws, LiveEvent(kind="out_tr", text="네, 좋아요."), st)
    await cs._forward_event(ws, LiveEvent(kind="audio", audio=b"\x00" * 960), st)
    tr = [p for k, p in ws.frames if k == "text" and p.get("type") == "output_transcript"]
    assert [p["text"] for p in tr] == ["네, 좋아요."]
    assert all(p.get("text") == "" for k, p in ws.frames if k == "text" and p.get("type") == "sentence")


@pytest.mark.asyncio
async def test_clip_emit_pcm_orders_text_audio_end():
    from domains.learning.realtime import clip_expr

    class _S:
        _play_until = 0.0

    s = _S()
    s._out = __import__("asyncio").Queue()
    await clip_expr.ClipExprSession._emit_pcm(s, b"\x00" * (clip_expr._CHUNK + 10), "안녕하세요", "neutral", "neutral")
    ev = []
    while not s._out.empty():
        ev.append(s._out.get_nowait())
    assert [(e.kind, e.sync, e.text) for e in ev if e.kind == "out_tr"] == [("out_tr", True, "안녕하세요"), ("out_tr", True, "")]
    assert [e.kind for e in ev] == ["out_tr", "audio", "audio", "out_tr"]
