"""음성 클립 통화 시험(PM-DEC-856 · exp/clip-call) — 꺼짐이면 종전과 같고, 켜면 클립이 모델 오디오 앞(T1)·턴 끝 앞(T2)에 나간다."""
from __future__ import annotations

import json

import pytest

import domains.learning.realtime.call_session as cs
from core.gemini_live import SET_FACE_TOOL, LiveEvent, set_face_tool
from domains.learning.realtime import clip_call
from domains.learning.realtime.expression_practice import ExpressionPractice


class _WS:
    def __init__(self):
        self.frames: list = []

    async def send_text(self, text: str) -> None:
        self.frames.append(("text", json.loads(text)))

    async def send_bytes(self, data: bytes) -> None:
        self.frames.append(("bytes", len(data)))


class _Sess:
    def __init__(self, script):
        self._script = list(script)
        self.notes: list[str] = []

    async def send_audio(self, pcm16_16k: bytes) -> None: ...

    async def send_text_turn(self, text: str) -> None: ...

    async def send_reground(self, text: str, *, turn_complete: bool = True) -> None:
        self.notes.append(text)

    async def send_tool_response(self, fn_id, fn_name, *, resume=False, blocking=False) -> None: ...

    async def events(self):
        for e in self._script:
            yield e


def _baba(locale="en"):
    return clip_call.for_call("Fenrir", locale, enabled="on")


def test_off_means_no_clip_and_the_tool_declaration_is_unchanged():
    assert clip_call.for_call("Fenrir", "en", enabled="") is None
    assert clip_call.for_call("Kore", "en", enabled="on") is None, "클립이 없는 캐릭터"
    assert clip_call.for_call("Leda", "en", enabled="Baba") is None, "고른 묶음만"
    assert set_face_tool() is SET_FACE_TOOL


def test_banks_load_in_live_format_and_english_reactions_need_an_english_learner():
    c = _baba()
    assert c.bank_name == "Baba" and len(c.bank) == 51
    assert all(len(x.pcm) % 2 == 0 and x.pcm for x in c.bank.values())
    assert {"laugh", "comfort", "try_it"} <= set(c.reaction_names())
    ko = _baba("ja")
    assert "try_it" not in ko.reaction_names() and "laugh" in ko.reaction_names()
    b = clip_call.for_call("Leda", "en", enabled="on")
    assert b.bank_name == "Bibi" and len(b.bank) == 48 and "nice" in b.reaction_names()


def test_declaration_carries_the_reaction_enum_only_when_asked():
    c = _baba()
    tool = set_face_tool(c.reaction_names(), c.reaction_description())
    props = tool.function_declarations[0].parameters.properties
    assert set(props) == {"emotion", "reaction"} and props["reaction"].enum == c.reaction_names()
    assert tool.function_declarations[0].parameters.required == ["emotion"], "reaction 은 선택"
    assert "[반응 소리]" in c.prompt_rule()


def test_variants_rotate_and_the_same_clip_never_plays_twice_in_a_row():
    c = _baba()
    ids = []
    for _ in range(8):
        x = c.pick_reaction("laugh")
        c.note_played(x)
        ids.append(x.id)
    assert all(a != b for a, b in zip(ids, ids[1:]))
    one = c.pick_reaction("not_quite")
    c.note_played(one)
    assert c.pick_reaction("not_quite") is None and c.skipped == 1, "변형 하나뿐 · 직전과 같으면 건너뛴다"


@pytest.mark.asyncio
async def test_reaction_before_the_turn_opens_goes_ahead_of_the_model_audio(monkeypatch):
    monkeypatch.setattr(cs._settings, "LIVE_FACE_SPIKE", True, raising=False)
    st = cs._CallState()
    st.learner_spoke = True
    st.clip = _baba()
    ws = _WS()
    script = [
        LiveEvent(kind="tool_call", fn_name="set_face", fn_id="f1", fn_args={"emotion": "laugh", "reaction": "laugh"},
                  auto_acked=True),
        LiveEvent(kind="out_tr", text="그것도 몰라?"),
        LiveEvent(kind="audio", audio=b"\x01\x00" * 10),
        LiveEvent(kind="turn_end"),
    ]
    with pytest.raises(cs._CallFinished):
        await cs._pump_gemini_to_client(ws, _Sess(script), st)
    kinds = [(k, p.get("type") if k == "text" else p) for k, p in ws.frames]
    first_bytes = next(i for i, f in enumerate(kinds) if f[0] == "bytes")
    model_bytes = max(i for i, f in enumerate(kinds) if f == ("bytes", 20))
    assert kinds.index(("text", "turn_start")) < first_bytes < model_bytes, kinds
    clip_bytes = sum(p for k, p in ws.frames[:model_bytes] if k == "bytes")
    assert clip_bytes == len(st.clip.bank[st.clip.played[0]].pcm), "클립 전체가 모델 오디오보다 먼저"
    assert st.clip.played and st.clip.played[0].startswith("A_laugh_")
    subs = [p["text"] for k, p in ws.frames if k == "text" and p.get("type") == "output_transcript"]
    assert subs[0] == st.clip.bank[st.clip.played[0]].text + " " and subs[-1] == "그것도 몰라?"


@pytest.mark.asyncio
async def test_greeting_turn_reaction_is_dropped(monkeypatch):
    monkeypatch.setattr(cs._settings, "LIVE_FACE_SPIKE", True, raising=False)
    st = cs._CallState()
    st.clip = _baba()
    script = [LiveEvent(kind="tool_call", fn_name="set_face", fn_id="f1", fn_args={"emotion": "laugh", "reaction": "laugh"},
                        auto_acked=True), LiveEvent(kind="audio", audio=b"\x01\x00"), LiveEvent(kind="turn_end")]
    with pytest.raises(cs._CallFinished):
        await cs._pump_gemini_to_client(_WS(), _Sess(script), st)
    assert st.clip.played == [] and st.clip.pending == []


def _practice():
    items = [{"obj": "안녕하세요?"}, {"obj": "만나서 반갑습니다"}]
    p = ExpressionPractice(items, language="ko")
    p.phase = "repeat"
    return items, p


@pytest.mark.asyncio
async def test_ask_clip_is_armed_on_the_repeat_answer_and_sent_before_turn_end(monkeypatch):
    monkeypatch.setattr(cs, "TURN_NOTE_MODE", "on", raising=False)
    monkeypatch.setattr(cs, "REGROUND_MODE", "on_user_turn", raising=False)
    monkeypatch.setattr(cs, "REGROUND_ATTACH_AT", "mic_open", raising=False)
    st = cs._CallState()
    st.learner_spoke = True
    st.clip = _baba()
    st.expr_items, st.expr_practice = _practice()
    st.turn_note = cs.TURN_NOTE_EXPRESSION
    st.beaver_turns = 2
    sess = _Sess([])
    await cs._attach_turn_note(sess, st, "마이크")
    assert sess.notes and sess.notes[0].endswith(cs._CLIP_ASK_NOTE) and st.clip.ask_armed == 1
    # 학습자 답 → 비버 턴 시작(flush 가 추적기를 다음 항목 introduce 로) → 짧은 반응 → turn_end
    st.cur_user_text.append("안녕하세요")
    ws = _WS()
    script = [LiveEvent(kind="out_tr", text="좋아."), LiveEvent(kind="audio", audio=b"\x01\x00" * 4), LiveEvent(kind="turn_end")]
    with pytest.raises(cs._CallFinished):
        await cs._pump_gemini_to_client(ws, _Sess(script), st)
    assert st.expr_practice.index == 1 and st.expr_practice.phase == "introduce"
    assert st.clip.played == ["B_ask_2_1"], st.clip.played
    kinds = [(k, p.get("type") if k == "text" else p) for k, p in ws.frames]
    assert max(i for i, f in enumerate(kinds) if f[0] == "bytes") < kinds.index(("text", "turn_end"))


@pytest.mark.asyncio
async def test_ask_clip_is_skipped_when_the_beaver_already_asked(monkeypatch):
    st = cs._CallState()
    st.clip = _baba()
    st.expr_items, st.expr_practice = _practice()
    st.expr_practice.index, st.expr_practice.phase = 1, "introduce"
    st.clip.ask_armed = 1
    st.turn_id = "t1"
    st.cur_beaver_text.append('Okay. How do you say "Nice to meet you" in Korean?')
    await cs._clip_turn_end(_WS(), st)
    assert st.clip.played == [] and st.clip.ask_armed is None


def test_no_ask_arming_outside_the_repeat_phase():
    st = cs._CallState()
    st.clip = _baba()
    st.expr_items, st.expr_practice = _practice()
    st.expr_practice.phase = "original"
    assert cs._clip_ask_target(st) is None
    st.expr_practice.phase = "repeat"
    assert cs._clip_ask_target(st) == 1
    st.clip = _baba("ja")
    assert cs._clip_ask_target(st) is None, "출제 클립은 영어 — 모국어 en 학습자만"
