# -*- coding: utf-8 -*-
"""T22 — Gemini 쪽 비정상 끊김(1006) 뒤 같은 통화에 2세대를 붙인다 (2026-09-12, 외부 의존 0).

설계문 docs/20260912_0010_통화-T22-Gemini측-끊김-재연결.md. 통화 1418: Vertex TCP 리셋 → APIError 1006 → 브리지 종료.
⛔ R4 무수정 — 여기서 보는 것은 `_run_session` 의 «최대 2세대» 루프와 그 조건 ①~⑤·분류 함수뿐이다.
① 2세대가 열리고 브리프 1턴이 시드로 나감 · segments·covered·expr_quiz 상태 유지 · usage_json reconnects=1
② 앱 끊김(_ClientDisconnect)은 재연결 안 함  ③ 2회째 끊김은 그대로 종료(상한)  ④ 남은 시간 <20s 면 안 함
⑤ should_close 뒤엔 안 함  ⑥ 분류 함수: 1006/ConnectionClosed/ConnectionReset 참 · WebSocketDisconnect·ValueError 거짓
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from google.genai import errors as genai_errors

import domains.learning.realtime.call_session as cs
from core.config import settings as app_settings
from core.gemini_live import LiveEvent
from core.prompts.common import CONTROL_TAG


def _api_error_1006() -> genai_errors.APIError:
    return genai_errors.APIError(1006, {"error": {"message": "abnormal closure (no close frame received or sent)", "status": "1006"}})


class _HangingWS:
    """앱 소켓 — 살아 있고 조용하다(마이크 프레임 없음). 끊기지 않는다."""

    def __init__(self):
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        from starlette.websockets import WebSocketState
        self.client_state = WebSocketState.CONNECTED

    async def receive(self) -> dict:
        await asyncio.Event().wait()
        return {}

    async def send_text(self, t: str) -> None:
        self.sent_text.append(t)

    async def send_bytes(self, b: bytes) -> None:
        self.sent_bytes.append(b)

    async def close(self, code: int = 1000) -> None:
        pass


class _DisconnectingWS(_HangingWS):
    async def receive(self) -> dict:
        await asyncio.sleep(0.02)
        return {"type": "websocket.disconnect"}


class _Session:
    def __init__(self, script: str):
        self.script = script          # "raise1006" | "normal" | "one_turn_then_raise"
        self.sent_text_turns: list[str] = []
        self.sent_audio: list[bytes] = []

    async def send_audio(self, pcm: bytes) -> None:
        self.sent_audio.append(pcm)

    async def send_text_turn(self, text: str) -> None:
        self.sent_text_turns.append(text)

    async def send_reground(self, text: str, *, turn_complete: bool = True) -> None:
        self.sent_text_turns.append(text)

    async def events(self):
        if self.script == "raise1006":
            await asyncio.sleep(0.02)
            raise _api_error_1006()
        if self.script == "hang":
            await asyncio.Event().wait()
        if self.script == "one_turn_then_raise":
            yield LiveEvent(kind="out_tr", text="Say 도와주세요.")
            yield LiveEvent(kind="audio", audio=b"\x00\x00" * 8)
            yield LiveEvent(kind="turn_end")
            await asyncio.sleep(0.02)
            raise _api_error_1006()
        yield LiveEvent(kind="out_tr", text="Okay, next one.")
        yield LiveEvent(kind="audio", audio=b"\x00\x00" * 8)
        yield LiveEvent(kind="turn_end")
        # 제너레이터 종료 → _CallFinished(정상 종료 신호)


def _factory(scripts: list[str], holder: dict):
    @contextlib.asynccontextmanager
    async def _f(client, settings, *, system_instruction, voice, **_kw):
        idx = holder.setdefault("opened", 0)
        holder["opened"] = idx + 1
        sess = _Session(scripts[min(idx, len(scripts) - 1)])
        holder.setdefault("sessions", []).append(sess)
        yield sess
    return _f


def _state(*, expression: bool = True) -> cs._CallState:
    st = cs._CallState()
    loop = asyncio.get_running_loop()
    st.call_start_ts = loop.time()
    st.call_duration_s = 300.0
    if expression:
        st.expr_items = [
            {"item_id": 11, "obj": "이거 얼마예요?", "des": "How much is it?", "ex": None},
            {"item_id": 12, "obj": "잘 부탁드립니다", "des": "Please take care", "ex": None},
            {"item_id": 13, "obj": "도와주세요", "des": "Please help me", "ex": None},
        ]
        st.reground_items = [i["obj"] for i in st.expr_items]
        st.expr_ctx = {"client": object(), "model": "m", "locale_label": "영어(English)", "target_language": "한국어"}
        st.reground_persona = ("선생님", "다정함")
        st.covered_nums = [1, 2]
        st.expr_quiz_pass = {11}
        st.expr_quiz_fail = {12}
        st.segments = [{"turn_index": 0, "role": "beaver", "text": "How much is it?", "pcm": b""},
                       {"turn_index": 1, "role": "user", "text": "이거 얼마예요?", "pcm": b""}]
        st.next_turn_index = 2
    return st


async def _run(st, ws, factory, seed="[선톡]"):
    return await cs._run_session(
        ws, state=st, system_instruction="SI", voice="Leda", seed_text=seed, settings=app_settings,
        client=object(), live_session_factory=factory, db_session_factory=None, call_id=1, member_id=1,
    )


# --------------------------------------------------------------------------- #
# ① 1006 → 2세대 · 브리프 1턴 · 상태 유지 · reconnects=1
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_gemini_1006_opens_a_second_generation_with_a_resume_brief() -> None:
    st = _state()
    st.expr_quiz_open = True
    st.expr_quiz_set = [1, 2, 3]
    st.expr_quiz_open_seg = 2
    holder: dict = {}
    ws = _HangingWS()
    with pytest.raises(cs._CallFinished):
        await _run(st, ws, _factory(["raise1006", "normal"], holder))
    assert holder["opened"] == 2 and st.session_epoch == 2 and st.reconnects == 1
    gen1, gen2 = holder["sessions"]
    assert gen1.sent_text_turns == ["[선톡]"], "1세대는 선톡 그대로"
    assert len(gen2.sent_text_turns) == 1
    brief = gen2.sent_text_turns[0]
    assert brief.startswith(CONTROL_TAG + " 연결이 잠깐 끊겼다가 이어졌다. 끊긴 것을 사과하지 말고")
    assert "[선톡]" not in brief, "재연결 세대에 선톡을 다시 보내면 비버가 또 인사한다"
    assert "이거 얼마예요?" in brief and "잘 부탁드립니다" in brief, "재접지 쪽지 재료(다룬 것·맞힌 것·틀린 것)"
    assert "퀴즈 중이다 — 남은 문항: «도와주세요»" in brief, "열린 퀴즈의 미판정 문항"
    # 상태 유지 — 세그먼트·covered·퀴즈·시계
    assert st.covered_nums == [1, 2] and st.expr_quiz_pass == {11} and st.expr_quiz_fail == {12}
    assert st.expr_quiz_open is True and st.expr_quiz_set == [1, 2, 3]
    assert [s["text"] for s in st.segments[:2]] == ["How much is it?", "이거 얼마예요?"]
    assert st.call_start_ts is not None
    # usage_json — reconnects 가 하드코딩 0 이 아니라 실제 값이다
    from types import SimpleNamespace
    cs._record_usage(st, SimpleNamespace(prompt_token_count=100, response_token_count=10, total_token_count=110,
                                         thoughts_token_count=0, prompt_tokens_details=None, response_tokens_details=None))
    uj = cs._usage_summary(st)
    assert uj["reconnects"] == 1 and uj["epochs"] == 2


@pytest.mark.asyncio
async def test_an_open_beaver_turn_is_flushed_before_the_second_generation() -> None:
    """열린 비버 턴의 자막은 세그먼트로 flush 하고 버린다 — 이미 나간 오디오는 클라가 재생한다."""
    st = _state()
    holder: dict = {}
    with pytest.raises(cs._CallFinished):
        await _run(st, _HangingWS(), _factory(["one_turn_then_raise", "normal"], holder))
    texts = [s["text"] for s in st.segments if s["role"] == "beaver"]
    assert "Say 도와주세요." in texts and "Okay, next one." in texts
    assert st.turn_id is None and st.cur_beaver_text == [] and st.user_turn_open is False


# --------------------------------------------------------------------------- #
# ② 앱 끊김은 재연결 안 함
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_an_app_disconnect_is_not_a_reconnect() -> None:
    st = _state()
    holder: dict = {}
    with pytest.raises(cs._ClientDisconnect):
        await _run(st, _DisconnectingWS(), _factory(["hang"], holder))
    assert holder["opened"] == 1 and st.reconnects == 0


# --------------------------------------------------------------------------- #
# ③ 2회째 끊김은 그대로 종료(상한 1)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_second_closure_propagates_the_error() -> None:
    st = _state()
    holder: dict = {}
    with pytest.raises(BaseExceptionGroup) as ei:
        await _run(st, _HangingWS(), _factory(["raise1006", "raise1006"], holder))
    assert holder["opened"] == 2 and st.reconnects == 1
    assert any(isinstance(e, genai_errors.APIError) for e in cs._leaf_exceptions(ei.value))


# --------------------------------------------------------------------------- #
# ④ 남은 시간 <20s · ⑤ 종료 구간 — 조건 함수
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_no_reconnect_when_less_than_20s_remain() -> None:
    st = _state()
    now = asyncio.get_running_loop().time()
    st.call_start_ts = now - (st.call_duration_s - 10.0)          # 10초 남음
    eg = BaseExceptionGroup("g", [_api_error_1006()])
    assert cs._reconnect_refusal(st, eg, now).startswith("남은 시간")
    st.call_start_ts = now - (st.call_duration_s - 60.0)          # 60초 남음
    assert cs._reconnect_refusal(st, eg, now) == ""
    holder: dict = {}
    st.call_start_ts = now - (st.call_duration_s - 10.0)
    with pytest.raises(BaseExceptionGroup):
        await _run(st, _HangingWS(), _factory(["raise1006", "normal"], holder))
    assert holder["opened"] == 1 and st.reconnects == 0


@pytest.mark.asyncio
async def test_no_reconnect_after_close_was_requested() -> None:
    st = _state()
    now = asyncio.get_running_loop().time()
    eg = BaseExceptionGroup("g", [_api_error_1006()])
    st.should_close = True
    assert cs._reconnect_refusal(st, eg, now) == "종료 구간"
    st.should_close, st.close_seed_sent = False, True
    assert cs._reconnect_refusal(st, eg, now) == "종료 구간"
    st.close_seed_sent = False
    st.reconnects = 1
    assert "상한" in cs._reconnect_refusal(st, eg, now)


# --------------------------------------------------------------------------- #
# ⑥ 분류 함수
# --------------------------------------------------------------------------- #
def test_gemini_side_closure_classification() -> None:
    from starlette.websockets import WebSocketDisconnect
    from websockets.exceptions import ConnectionClosedError

    assert cs._is_gemini_closure_leaf(_api_error_1006())
    assert cs._is_gemini_closure_leaf(genai_errors.APIError(1011, {"error": {"message": "internal error"}}))
    assert cs._is_gemini_closure_leaf(ConnectionClosedError(None, None))
    assert cs._is_gemini_closure_leaf(ConnectionResetError("reset"))
    e104 = OSError(104, "Connection reset by peer")
    assert cs._is_gemini_closure_leaf(e104)
    assert not cs._is_gemini_closure_leaf(WebSocketDisconnect(1001))
    assert not cs._is_gemini_closure_leaf(ValueError("x"))
    assert not cs._is_gemini_closure_leaf(cs._ClientDisconnect())
    assert not cs._is_gemini_closure_leaf(genai_errors.APIError(400, {"error": {"message": "bad request"}}))
    # 봉투: 전부 Gemini 쪽이어야 참 — 하나라도 다른 종류면 진짜 오류다
    assert cs._is_gemini_side_closure(BaseExceptionGroup("g", [_api_error_1006()]))
    assert cs._is_gemini_side_closure(BaseExceptionGroup("g", [_api_error_1006(), ConnectionResetError()]))
    assert not cs._is_gemini_side_closure(BaseExceptionGroup("g", [_api_error_1006(), ValueError("x")]))
    assert not cs._is_gemini_side_closure(BaseExceptionGroup("g", [WebSocketDisconnect(1001)]))
    assert not cs._is_gemini_side_closure(BaseExceptionGroup("g", [asyncio.CancelledError()]))


def test_the_reconnect_brief_for_a_normal_call_carries_the_last_beaver_line() -> None:
    st = cs._CallState()
    st.segments = [{"turn_index": 0, "role": "beaver", "text": "어제 뭐 했어요?", "pcm": b""},
                   {"turn_index": 1, "role": "user", "text": "집에 있었어요", "pcm": b""}]
    brief = cs._reconnect_brief(st)
    assert brief.startswith(CONTROL_TAG) and "어제 뭐 했어요?" in brief
    assert "사과하지 말고" in brief and "인사도 다시 하지 말고" in brief
