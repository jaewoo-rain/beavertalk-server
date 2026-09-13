"""표정 초기화(2026-09-14 D, 실통화 1602 조각2 set_face(happy) 뒤 여러 턴 웃는 채 고정) — 감정 판정이 아니다.

새 비버 턴(turn_start)에 직전 turn_end 이후 set_face 가 없었으면 서버가 sentence{emotion:neutral} 마커 1개를 오디오 앞에 보낸다. 있었으면 0개.
"""
from __future__ import annotations

import json

import pytest

import domains.learning.realtime.call_session as cs
from core.gemini_live import LiveEvent


class _WS:
    def __init__(self):
        self.frames: list = []          # (kind, payload) — 순서가 곧 계약

    async def send_text(self, text: str) -> None:
        self.frames.append(("text", json.loads(text)))

    async def send_bytes(self, data: bytes) -> None:
        self.frames.append(("bytes", len(data)))


def _markers(ws):
    return [p for k, p in ws.frames if k == "text" and p.get("type") == "sentence"]


async def _turn(ws, st, *, audio=True):
    """비버 한 턴: (out_tr) → audio → turn_end."""
    await cs._forward_event(ws, LiveEvent(kind="out_tr", text="네, 좋아요."), st)
    if audio:
        await cs._forward_event(ws, LiveEvent(kind="audio", audio=b"\x00\x00" * 8), st)
    await cs._forward_event(ws, LiveEvent(kind="turn_end"), st)
    cs._flush_beaver_segment(st)


@pytest.mark.asyncio
async def test_a_turn_without_set_face_gets_one_neutral_marker_before_its_audio():
    st = cs._CallState()
    st.learner_spoke = True
    ws = _WS()
    await _turn(ws, st)
    m = _markers(ws)
    assert len(m) == 1 and m[0]["emotion"] == "neutral" and m[0]["seq"] == 1 and m[0]["text"] == ""
    kinds = [(k, p.get("type") if k == "text" else "bytes") for k, p in ws.frames]
    assert kinds.index(("text", "sentence")) < kinds.index(("bytes", "bytes")), "마커는 오디오 앞"
    assert kinds.index(("text", "turn_start")) < kinds.index(("text", "sentence"))
    assert st.face_last == "neutral" and st.face_last_pcm == -1, "같은자리 판정 재료는 건드리지 않는다"
    # 두 번째 턴도 set_face 가 없으면 또 1개(턴마다 초기화) — 통화 seq 는 이어진다
    await _turn(ws, st)
    assert [x["seq"] for x in _markers(ws)] == [1, 2]


@pytest.mark.asyncio
async def test_a_turn_with_set_face_gets_no_neutral_marker():
    st = cs._CallState()
    st.learner_spoke = True
    ws = _WS()
    st.face_called_this_turn = True          # 직전 turn_end 뒤 모델이 set_face 를 불렀다(펌프의 tool_call 분기가 세운다 — 보냈든 중복 억제든)
    await _turn(ws, st)
    assert _markers(ws) == [], "set_face 가 있었던 턴엔 초기화 0개"
    assert st.face_called_this_turn is False, "flush 가 다음 턴을 위해 비운다"
    await _turn(ws, st)
    assert len(_markers(ws)) == 1 and _markers(ws)[0]["emotion"] == "neutral", "그 다음 턴에 set_face 가 없으면 다시 1개"


@pytest.mark.asyncio
async def test_no_reset_marker_before_the_learner_has_spoken():
    st = cs._CallState()                      # learner_spoke False — 인사 구간엔 마커를 보내지 않는 기존 규율 그대로
    ws = _WS()
    await _turn(ws, st)
    assert _markers(ws) == []
