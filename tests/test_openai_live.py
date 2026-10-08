"""OpenAI Realtime 어댑터 단위 시험 — 외부 의존 0(WS 를 열지 않는다).

잠그는 것:
  ① 16k→24k 리샘플: 길이·클리핑·프레임 경계 연속성·DC 오프셋
  ② session.update 페이로드: 24k 입출력 동일 · semantic_vad · 표정 ON/OFF · truncation 부재
  ③ 이벤트 정규화: 9종 · `go_away`/`resume_update` **영영 안 올린다** · 툴 전용 응답은 turn_end 건너뜀
  ④ usage shim: `_record_usage`/`_modality_pairs` 가 **그대로** 먹는다 + 캐시 분해
  ⑤ 이벤트 필드가 호출부 계약(`LiveEvent`)과 어긋나지 않는다
  ⑥ 동시 통화 1건 게이트
  ⑦ 툴 자동응답 왕복(`{"emotion":"happy"}`)
"""
from __future__ import annotations

import asyncio
import base64
import json
import struct

import pytest

from core.openai import audio as oa_audio
from core.openai import session as oa
from core.openai import tools as oa_tools


# --------------------------------------------------------------------------- #
# ① 리샘플
# --------------------------------------------------------------------------- #
def _pcm(samples) -> bytes:
    return struct.pack("<%dh" % len(samples), *samples)


def _unpack(raw: bytes) -> list[int]:
    return list(struct.unpack("<%dh" % (len(raw) // 2), raw))


def test_upsample_length_converges_to_one_and_a_half():
    """긴 스트림에서 출력 길이는 입력의 1.5배에 수렴한다(프레임마다 ±1샘플)."""
    up = oa_audio.Upsampler16kTo24k()
    total_in = 0
    total_out = 0
    for _ in range(100):                       # 20ms 프레임 100개 = 2초
        frame = _pcm([0] * 320)
        total_in += len(frame)
        total_out += len(up.feed(frame))
    ratio = total_out / total_in
    assert 1.49 < ratio < 1.51, ratio
    # 2초 입력 → 24k·16bit 로 약 2초
    assert abs(total_out / oa_audio.TARGET_BYTES_PER_S - 2.0) < 0.01


def test_upsample_never_clips_and_stays_in_range():
    """선형보간은 두 값의 볼록결합 ⇒ int16 범위를 **원리적으로** 못 벗어난다."""
    up = oa_audio.Upsampler16kTo24k()
    extreme = [32767, -32768] * 160
    out = _unpack(up.feed(_pcm(extreme)))
    assert out, "출력이 비었다"
    assert min(out) >= -32768 and max(out) <= 32767
    assert max(out) <= max(extreme) and min(out) >= min(extreme)


def test_upsample_is_exact_on_dc_signal():
    """상수 신호는 보간해도 상수다 — ⛔ 음수에서 DC 오프셋이 생기면 여기서 잡힌다."""
    for value in (1000, -1000, -1, 1, -32768, 32767):
        up = oa_audio.Upsampler16kTo24k()
        out = _unpack(up.feed(_pcm([value] * 320)))
        assert set(out) == {value}, (value, sorted(set(out))[:5])


def test_upsample_ramp_is_monotonic_across_frame_boundaries():
    """경계에서 클릭(값 점프·역행)이 없다 — 직전 샘플을 들고 가는지 보는 시험."""
    up = oa_audio.Upsampler16kTo24k()
    out: list[int] = []
    cur = 0
    for _ in range(5):
        frame = list(range(cur, cur + 320))     # 계속 올라가는 램프
        cur += 320
        out += _unpack(up.feed(_pcm(frame)))
    assert out == sorted(out), "램프가 어디선가 역행했다(경계 불연속)"
    diffs = [b - a for a, b in zip(out, out[1:])]
    assert max(diffs) <= 1, max(diffs)          # 2:3 업샘플이면 증분은 0 또는 1


def test_upsample_handles_odd_byte_chunk_without_losing_a_sample():
    """샘플 중간에서 끊긴 바이트는 **버리지 않고** 다음 호출로 넘긴다."""
    up = oa_audio.Upsampler16kTo24k()
    raw = _pcm([500] * 10)
    first = up.feed(raw[:-1])                   # 마지막 바이트를 뺀다
    second = up.feed(raw[-1:])
    out = _unpack(first + second)
    assert out and set(out) == {500}


def test_upsample_empty_input_is_noop():
    up = oa_audio.Upsampler16kTo24k()
    assert up.feed(b"") == b""


# --------------------------------------------------------------------------- #
# ② 세션 설정
# --------------------------------------------------------------------------- #
def test_session_config_uses_24k_on_both_sides_and_semantic_vad():
    cfg = oa.build_session_config(system_instruction="지시", voice="marin")
    sess = cfg["session"]
    assert cfg["type"] == "session.update" and sess["type"] == "realtime"
    assert sess["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert sess["audio"]["output"]["format"] == sess["audio"]["input"]["format"], \
        "입·출력 포맷은 같아야 한다(세션 중 변경 불가)"
    assert sess["audio"]["input"]["turn_detection"]["type"] == "semantic_vad", \
        "기본 server_vad(500ms)는 학습자의 쉼을 전부 턴으로 쪼갰다(실측)"
    assert sess["audio"]["output"]["voice"] == "marin"
    assert sess["output_modalities"] == ["audio"]


def test_session_config_has_no_truncation_or_compression_knobs():
    """⛔ 압축·truncation 을 켜지 않는다 — 켜면 캐시 프리픽스가 깨져 할인이 사라진다."""
    blob = json.dumps(oa.build_session_config(system_instruction="지시"))
    for word in ("truncation", "context_window", "compression", "session_resumption"):
        assert word not in blob, word


def test_session_config_face_tool_on_off():
    off = oa.build_session_config(system_instruction="지시", with_face_tool=False)["session"]
    assert off["tools"] == [] and off["tool_choice"] == "none"
    on = oa.build_session_config(system_instruction="지시", with_face_tool=True)["session"]
    assert on["tool_choice"] == "auto" and len(on["tools"]) == 1
    tool = on["tools"][0]
    # ⭐ OpenAI 툴 스키마는 **평평하다**(Chat Completions 처럼 한 겹 감싸면 안 받는다).
    assert tool["type"] == "function" and tool["name"] == "set_face"
    assert "function" not in tool
    # ⭐ 인자 키는 `emotion` — 프론트 계약(`protocol.py` ServerSentenceMarker.emotion)이다.
    assert list(tool["parameters"]["properties"]) == ["emotion"]
    assert tool["parameters"]["properties"]["emotion"]["enum"] == [
        "happy", "surprised", "sad", "angry", "laugh"]
    assert "neutral" not in tool["parameters"]["properties"]["emotion"]["enum"], \
        "앱이 클립 뒤 스스로 idle 로 돌아온다 — 복귀 호출은 원가를 2배로 만든다"


def test_session_config_transcription_language_from_codes():
    cfg = oa.build_session_config(system_instruction="지시",
                                  input_language_codes=["ko-KR", "en-US"])
    tr = cfg["session"]["audio"]["input"]["transcription"]
    assert tr["language"] == "ko"
    assert tr["model"]
    # 전사를 끌 수도 있어야 한다(모델 이름이 거부될 때의 폴백 경로)
    off = oa.build_session_config(system_instruction="지시", transcribe_model=None)
    assert "transcription" not in off["session"]["audio"]["input"]


# --------------------------------------------------------------------------- #
# ③ 이벤트 정규화
# --------------------------------------------------------------------------- #
class _FakeWS:
    """스크립트된 서버 이벤트를 돌려주는 가짜 WS. 보낸 것은 기록한다."""

    def __init__(self, script: list[dict]):
        self._script = list(script)
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self._script:
            import websockets
            raise websockets.exceptions.ConnectionClosedOK(None, None)
        return json.dumps(self._script.pop(0))


async def _drain(script: list[dict]):
    ws = _FakeWS(script)
    sess = oa.OpenAIRealtimeSession(ws)
    out = []
    async for ev in sess.events():
        out.append(ev)
    return out, ws, sess


@pytest.mark.asyncio
async def test_events_normalize_audio_transcripts_and_turn_end():
    pcm = b"\x01\x02" * 8
    evs, _ws, _s = await _drain([
        {"type": "response.output_audio_transcript.delta", "delta": "안녕"},
        {"type": "response.output_audio.delta", "delta": base64.b64encode(pcm).decode()},
        {"type": "conversation.item.input_audio_transcription.delta", "delta": "고마"},
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "고마워요"},
        {"type": "response.done", "response": {"status": "completed", "output": [
            {"type": "message"}], "usage": {"input_tokens": 10, "output_tokens": 3}}},
    ])
    kinds = [e.kind for e in evs]
    # ⭐ 입력 전사는 **델타만** 올린다(2026-10-07, call 1759·1764). 벤더가 델타와
    #   `completed`(그 턴의 전문)를 둘 다 주는데, 호출부는 받은 조각을 전부 쌓아
    #   `"".join` 하므로 둘 다 올리면 전사가 **정확히 2배**가 된다.
    #   ⛔ 합계(`completed`)를 버리고 흘리는 쪽을 남긴다 — 끊으면 학습자 자막이 문장
    #     끝에야 떠서 체감이 느려진다(1764 에서 사장님이 바로 잡아냈다).
    assert kinds == ["out_tr", "audio", "in_tr", "usage", "turn_end"]
    assert evs[1].audio == pcm, "출력 오디오는 변환 없이 그대로 올린다(PCM24k)"
    assert evs[2].text == "고마" and evs[2].is_final is False, \
        "자막은 델타로 흘려야 한다 — 전문 한 번으로 바꾸면 체감이 느려진다"


@pytest.mark.asyncio
async def test_events_never_yield_go_away_or_resume_update():
    """⛔ 이 두 종류는 **영영** 안 올린다 — 호출부의 그 분기는 죽은 코드로 남는다."""
    evs, _ws, _s = await _drain([
        {"type": "session.created"},
        {"type": "session.updated"},
        {"type": "rate_limits.updated", "rate_limits": []},
        {"type": "error", "error": {"message": "x"}},
        {"type": "input_audio_buffer.speech_started"},
        {"type": "conversation.item.done"},
        {"type": "response.done", "response": {"status": "completed", "output": [{"type": "message"}]}},
    ])
    kinds = {e.kind for e in evs}
    assert "go_away" not in kinds and "resume_update" not in kinds
    assert all(e.time_left is None and e.resume_handle is None and e.resumable is False
               for e in evs)


@pytest.mark.asyncio
async def test_tool_only_response_does_not_close_the_turn():
    """툴 전용 응답(Response 1/2)의 done 은 turn_end 가 아니다 — 빈 턴을 만들면 안 된다."""
    evs, ws, _s = await _drain([
        {"type": "response.function_call_arguments.done", "call_id": "c1",
         "name": "set_face", "arguments": '{"emotion":"happy"}'},
        {"type": "response.done", "response": {"status": "completed", "output": [
            {"type": "function_call", "call_id": "c1", "name": "set_face",
             "arguments": '{"emotion":"happy"}'}],
            "usage": {"input_tokens": 100, "output_tokens": 5}}},
        {"type": "response.done", "response": {"status": "completed", "output": [
            {"type": "message"}], "usage": {"input_tokens": 110, "output_tokens": 40}}},
    ])
    kinds = [e.kind for e in evs]
    assert kinds.count("tool_call") == 1, "같은 call_id 를 두 번 올리면 안 된다"
    assert kinds.count("turn_end") == 1, "툴 전용 응답은 턴을 닫지 않는다"
    assert kinds.count("usage") == 2, "두 응답 모두 과금된다 — 둘 다 올려야 한다"
    tool = next(e for e in evs if e.kind == "tool_call")
    assert tool.fn_name == "set_face" and tool.fn_args == {"emotion": "happy"}
    assert tool.auto_acked is True, "어댑터가 이미 답했다 — 소비측이 또 답하면 새 입력이 된다"
    # 자동응답 + 재개(response.create)가 실제로 나갔다
    sent = [m["type"] for m in ws.sent]
    assert "conversation.item.create" in sent and "response.create" in sent
    ack = next(m for m in ws.sent if m["type"] == "conversation.item.create")
    assert ack["item"]["type"] == "function_call_output" and ack["item"]["call_id"] == "c1"


@pytest.mark.asyncio
async def test_cancelled_response_becomes_interrupted():
    evs, _ws, _s = await _drain([
        {"type": "response.done", "response": {"status": "cancelled", "output": []}},
    ])
    assert [e.kind for e in evs] == ["interrupted"]


@pytest.mark.asyncio
async def test_connection_closed_ends_the_stream_without_raising():
    """⛔ 끊김을 예외로 올리지 않는다 — 올리면 호출부가 «저쪽 끊김 → 재연결» 로 읽는다."""
    evs, _ws, _s = await _drain([])
    assert evs == []


# --------------------------------------------------------------------------- #
# ④ usage shim — 호출부가 **그대로** 먹는다
# --------------------------------------------------------------------------- #
_USAGE = {
    "input_tokens": 3200, "output_tokens": 180, "total_tokens": 3380,
    "input_token_details": {
        "audio_tokens": 2900, "text_tokens": 300, "cached_tokens": 2560,
        "cached_tokens_details": {"audio_tokens": 2500, "text_tokens": 60},
    },
    "output_token_details": {"audio_tokens": 160, "text_tokens": 20},
}


def test_usage_shim_is_eaten_by_record_usage_unchanged():
    import domains.learning.realtime.call_session as cs

    state = cs._CallState()
    cs._record_usage(state, oa.usage_shim(_USAGE))
    assert len(state.usage_log) == 1
    row = state.usage_log[0]
    assert row["prompt"] == 3200 and row["resp"] == 180 and row["total"] == 3380
    assert row["cached"] == 2560
    assert dict(row["in_detail"]) == {"AUDIO": 2900, "TEXT": 300}
    assert dict(row["out_detail"]) == {"AUDIO": 160, "TEXT": 20}
    assert dict(row["cached_detail"]) == {"AUDIO": 2500, "TEXT": 60}
    summary = cs._usage_summary(state)
    assert summary["in_mod"] == {"AUDIO": 2900, "TEXT": 300}
    assert summary["cached_mod"] == {"AUDIO": 2500, "TEXT": 60}
    assert summary["sum_cached"] == 2560


def test_gemini_usage_summary_has_no_cached_mod_key():
    """⛔ Gemini 요약은 **바이트 동일** — 새 키가 생기면 안 된다(벤더가 분해를 안 준다)."""
    import domains.learning.realtime.call_session as cs

    class _FakeGeminiUsage:
        prompt_token_count = 100
        response_token_count = 10
        total_token_count = 110
        thoughts_token_count = 0
        cached_content_token_count = None
        tool_use_prompt_token_count = None
        prompt_tokens_details = None
        response_tokens_details = None

    state = cs._CallState()
    cs._record_usage(state, _FakeGeminiUsage())
    summary = cs._usage_summary(state)
    assert "cached_mod" not in summary


def test_usage_shim_none_when_no_usage():
    assert oa.usage_shim(None) is None and oa.usage_shim({}) is None


# --------------------------------------------------------------------------- #
# ⑤ 이벤트 필드가 호출부 계약과 어긋나지 않는다
# --------------------------------------------------------------------------- #
def test_openai_event_fields_cover_the_caller_contract():
    """⭐ 격리 규칙 때문에 어댑터는 `LiveEvent` 를 import 할 수 없다 — **시험이** 대조한다.

    ⛔ 이 시험이 없으면 Gemini 쪽에 필드가 하나 늘었을 때 OpenAI 통화에서만 조용히
      AttributeError 가 난다(호출부가 그 필드를 무조건 읽는 분기에서).
    """
    import dataclasses

    from core.gemini_live import LiveEvent      # 시험은 둘 다 import 해도 된다(패키지 밖이다)

    gemini = {f.name for f in dataclasses.fields(LiveEvent)}
    ours = {f.name for f in dataclasses.fields(oa.OpenAIRealtimeEvent)}
    missing = gemini - ours
    assert not missing, "호출부가 읽을 수 있는 필드가 우리 이벤트에 없다: %s" % missing


def test_session_satisfies_the_six_method_protocol():
    from core.gemini_live import LiveSessionProtocol

    sess = oa.OpenAIRealtimeSession(_FakeWS([]))
    assert isinstance(sess, LiveSessionProtocol)
    for name in ("send_audio", "send_reground", "send_text_turn", "send_persona",
                 "send_tool_response", "events"):
        assert callable(getattr(sess, name)), name


def test_open_session_signature_matches_the_gemini_factory():
    """⛔ 호출부가 kwargs 를 «값이 있을 때만» 조립한다 — 받는 이름이 같아야 한다."""
    import inspect

    from core import gemini_live

    g = inspect.signature(gemini_live.open_session.__wrapped__)
    o = inspect.signature(oa.open_session.__wrapped__)
    assert list(g.parameters) == list(o.parameters), (list(g.parameters), list(o.parameters))


# --------------------------------------------------------------------------- #
# ⑥⑦ 보내기 통로 · 동시 통화 게이트
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_send_audio_upsamples_and_never_commits():
    ws = _FakeWS([])
    sess = oa.OpenAIRealtimeSession(ws)
    await sess.send_audio(_pcm([100] * 320))
    assert len(ws.sent) == 1
    msg = ws.sent[0]
    assert msg["type"] == "input_audio_buffer.append"
    raw = base64.b64decode(msg["audio"])
    assert len(raw) > 640, "16k 를 그대로 보냈다 — OpenAI 는 16k 를 거부한다"
    assert all(v == 100 for v in _unpack(raw))
    assert not any(m["type"] == "input_audio_buffer.commit" for m in ws.sent), \
        "턴 경계는 semantic_vad 가 정한다 — 수동 commit 을 섞으면 턴이 두 번 끊긴다"


@pytest.mark.asyncio
async def test_reground_without_turn_complete_does_not_trigger_a_response():
    """⭐ 퀴즈 큐가 이 통로로 온다 — 항목만 덧붙이고 생성은 안 시킨다."""
    ws = _FakeWS([])
    sess = oa.OpenAIRealtimeSession(ws)
    await sess.send_reground("[안내] 3번을 물어라", turn_complete=False)
    assert [m["type"] for m in ws.sent] == ["conversation.item.create"]
    await sess.send_reground("지금 답해라", turn_complete=True)
    assert [m["type"] for m in ws.sent][-2:] == ["conversation.item.create", "response.create"]


@pytest.mark.asyncio
async def test_persona_only_appends_text_turn_also_creates_response():
    ws = _FakeWS([])
    sess = oa.OpenAIRealtimeSession(ws)
    await sess.send_persona("너는 비버다")
    assert [m["type"] for m in ws.sent] == ["conversation.item.create"]
    ws.sent.clear()
    await sess.send_text_turn("인사해라")
    assert [m["type"] for m in ws.sent] == ["conversation.item.create", "response.create"]
    item = ws.sent[0]["item"]
    assert item["role"] == "user" and item["content"][0]["type"] == "input_text"


@pytest.mark.asyncio
async def test_concurrency_gate_allows_only_one_session(monkeypatch):
    """⛔ 동시 통화 1건(사장님 결정 1) — 두 번째는 열리지 않는다."""
    class _S:
        GPT_API_KEY = "test-key"
        OPENAI_REALTIME_MODEL = "gpt-realtime-2.1-mini"
        OPENAI_REALTIME_VOICE = "marin"
        OPENAI_REALTIME_TRANSCRIBE_MODEL = ""
        OPENAI_REALTIME_MAX_OUTPUT_TOKENS = 0
        OPENAI_REALTIME_MAX_CONCURRENT = 1

    monkeypatch.setattr(oa, "_active_sessions", 1, raising=False)
    with pytest.raises(oa.OpenAIRealtimeBusy):
        async with oa.open_session(None, _S(), system_instruction="x"):
            pass
    assert oa.active_session_count() == 1, "거절이 카운터를 흔들면 안 된다"


@pytest.mark.asyncio
async def test_missing_key_is_a_clean_failure_and_never_logs_the_key(caplog):
    """⛔ 키가 없으면 세션 열기 실패로 끝낸다(호출부가 LIVE_START_FAILED 를 보낸다)."""
    class _S:
        GPT_API_KEY = ""
        OPENAI_REALTIME_MAX_CONCURRENT = 1

    import os

    saved = os.environ.pop("GPT_API_KEY", None)
    try:
        with pytest.raises(oa.OpenAIRealtimeError) as err:
            async with oa.open_session(None, _S(), system_instruction="x"):
                pass
    finally:
        if saved is not None:
            os.environ["GPT_API_KEY"] = saved
    assert "GPT_API_KEY" in str(err.value)
    # ⛔ 키 **값**이 예외·로그에 섞여서는 안 된다(마스킹해서도 안 찍는다).
    assert "test-key" not in str(err.value)
    assert oa.active_session_count() == 0
    del caplog


def test_face_rule_block_matches_the_tool_declaration():
    """선언과 규칙이 **같은 말**을 해야 한다 — 다르면 모델이 어느 쪽을 따를지 모른다."""
    rule = oa_tools.face_rule_block()
    tool = oa_tools.set_face_tool()
    assert "set_face" in rule and tool["name"] == "set_face"
    for key in ("감정이 드러나", "평범한 턴", "소리가 아니다"):
        assert key in rule, key
        assert key in tool["description"] or key in rule


@pytest.mark.asyncio
async def test_completed_transcript_is_never_forwarded():
    """⛔ `completed` 는 델타의 **합계**다 — 올리면 전사가 2배가 된다(call 1759 t2).

    빈 전사든 내용이 있든 똑같이 버린다. 자막은 델타가 책임진다.
    """
    evs, _ws, _s = await _drain([
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "   "},
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "네"},
    ])
    assert evs == [], "completed 를 올리면 델타와 합쳐져 전사가 2배가 된다"


@pytest.mark.asyncio
async def test_delta_then_completed_yields_the_text_exactly_once():
    """⭐ 1759 t2 「I don't know that.I don't know that.」 의 회귀 못."""
    evs, _ws, _s = await _drain([
        {"type": "conversation.item.input_audio_transcription.delta", "delta": "I don't "},
        {"type": "conversation.item.input_audio_transcription.delta", "delta": "know that."},
        {"type": "conversation.item.input_audio_transcription.completed",
         "transcript": "I don't know that."},
    ])
    assert "".join(e.text or "" for e in evs) == "I don't know that.", \
        "델타의 합이 곧 전문이어야 하고, 전문이 한 번 더 붙으면 2배가 된다"


@pytest.mark.asyncio
async def test_transcription_failure_is_logged_not_raised():
    evs, _ws, _s = await _drain([
        {"type": "conversation.item.input_audio_transcription.failed",
         "error": {"message": "asr down"}},
    ])
    assert evs == []


@pytest.mark.asyncio
async def test_tool_ack_is_sent_before_the_event_is_yielded():
    """⛔ yield 는 소비측 처리를 기다린다 — 그 뒤에 답하면 모델이 그만큼 턴을 멈춘다.

    Gemini 어댑터가 같은 자리에서 「두 번 말하기」를 겪고 「파싱 즉시 응답」으로 고쳤다.
    """
    ws = _FakeWS([
        {"type": "response.function_call_arguments.done", "call_id": "c9",
         "name": "set_face", "arguments": '{"emotion":"laugh"}'},
    ])
    sess = oa.OpenAIRealtimeSession(ws)
    agen = sess.events()
    first = await agen.__anext__()
    assert first.kind == "tool_call"
    # 소비측이 이 이벤트를 **받은 시점에 이미** ack 이 나가 있어야 한다.
    assert [m["type"] for m in ws.sent] == ["conversation.item.create", "response.create"]
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()


@pytest.mark.asyncio
async def test_rate_limits_are_logged_with_numbers(caplog):
    """⭐ TPM 40,000 이 이 엔진의 가장 큰 제약 — 잔량 로그가 유일한 계기판이다."""
    import logging

    caplog.set_level(logging.INFO, logger="core.openai.session")
    await _drain([{"type": "rate_limits.updated", "rate_limits": [
        {"name": "tokens", "limit": 40000, "remaining": 12345, "reset_seconds": 7.5}]}])
    line = next(r.getMessage() for r in caplog.records if "한도" in r.getMessage())
    assert "12345" in line and "40000" in line


@pytest.mark.asyncio
async def test_failed_response_is_logged_but_still_closes_the_turn(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="core.openai.session")
    evs, _ws, _s = await _drain([
        {"type": "response.done", "response": {
            "status": "incomplete", "output": [{"type": "message"}],
            "status_details": {"reason": "max_output_tokens"}}},
    ])
    assert [e.kind for e in evs] == ["turn_end"], "턴은 닫아야 한다(안 닫으면 펌프가 멈춘다)"
    assert any("max_output_tokens" in r.getMessage() for r in caplog.records)

# --------------------------------------------------------------------------- #
# 음색 — ⛔ DB 의 Gemini 음색이 그대로 가면 통화가 열리기도 전에 죽는다
#   실측(2026-10-05, call 1733·1734): voice=Fenrir → session.update 거부 →
#   LIVE_START_FAILED. 호출부는 DB 캐릭터 음색을 넘기고 그건 Gemini 이름이다.
# --------------------------------------------------------------------------- #
def test_a_gemini_voice_name_is_replaced_not_sent():
    """⛔ 이게 깨지면 모든 통화가 열리기 전에 죽는다 — 실제로 그렇게 죽었다."""
    from core.openai import session as sess

    class S:
        OPENAI_REALTIME_VOICE = "marin"

    # DB 의 Baba 음색(Gemini 이름)
    assert sess._pick_voice("Fenrir", S()) == "marin"
    # 그 밖의 Gemini 음색들도 같다
    for g in ("Puck", "Charon", "Kore", "Aoede", "Zephyr"):
        assert sess._pick_voice(g, S()) == "marin", g


def test_a_supported_voice_is_passed_through():
    from core.openai import session as sess

    class S:
        OPENAI_REALTIME_VOICE = "marin"

    for ok in sorted(sess.SUPPORTED_VOICES):
        assert sess._pick_voice(ok, S()) == ok, ok
    # 공백·None 이면 설정값
    assert sess._pick_voice("", S()) == "marin"
    assert sess._pick_voice(None, S()) == "marin"
    assert sess._pick_voice("  marin  ", S()) == "marin"


def test_a_bad_configured_voice_falls_back_to_the_default():
    """⚠ env 를 잘못 넣어도 통화는 열려야 한다 — 거기서 또 죽으면 원인이 두 겹이 된다."""
    from core.openai import session as sess

    class S:
        OPENAI_REALTIME_VOICE = "Fenrir"      # 설정값 자체가 틀린 경우

    assert sess._pick_voice("Fenrir", S()) == sess.DEFAULT_VOICE
    assert sess._pick_voice("", S()) == sess.DEFAULT_VOICE
    assert sess.DEFAULT_VOICE in sess.SUPPORTED_VOICES


def test_the_session_config_only_ever_carries_a_supported_voice():
    """`build_session_config` 가 실제로 내보내는 값까지 본다(헬퍼만 보면 반쪽이다)."""
    from core.openai import session as sess

    cfg = sess.build_session_config(system_instruction="x", voice="marin")
    assert cfg["session"]["audio"]["output"]["voice"] in sess.SUPPORTED_VOICES


# --------------------------------------------------------------------------- #
# 쪽지 role — ⭐ 이 한 글자가 되묻기 복종을 44% → 100% 로 바꿨다
#   (2026-10-06 하네스 83세션, p=0.00051). ⚠ **오디오에서만** 터진다 — 텍스트
#   모달리티에서는 user 로도 23/24 가 따랐다. 그래서 코드 시험으로는 안 보였고,
#   실통화 1740·1741 의 실패 경로가 글자까지 같게 재현됐다.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_server_notes_go_in_as_system_not_user():
    """⛔ 이게 user 로 돌아가면 퀴즈 큐·세트·드릴 안내가 절반쯤 무시된다."""
    ws = _FakeWS([])
    sess = oa.OpenAIRealtimeSession(ws)
    await sess.send_reground("[안내] 되묻는 차례다", turn_complete=False)
    items = [m for m in ws.sent if m["type"] == "conversation.item.create"]
    assert len(items) == 1, ws.sent
    assert items[0]["item"]["role"] == "system", (
        "서버 쪽지가 user 로 들어간다 — 오디오 턴에서 세션 지시문에 밀린다", items[0])


@pytest.mark.asyncio
async def test_the_other_two_channels_stay_user():
    """⛔ 선톡·종료·무음 넛지 시드는 **측정하지 않았다** — 안 쟀으면 안 바꾼다."""
    for meth in ("send_text_turn", "send_persona"):
        ws = _FakeWS([])
        sess = oa.OpenAIRealtimeSession(ws)
        await getattr(sess, meth)("x")
        items = [m for m in ws.sent if m["type"] == "conversation.item.create"]
        assert items and items[0]["item"]["role"] == "user", (meth, items)


# ── 이중발화 가드(2026-10-07, call 1758 t23) ──────────────────────────────────── #
#   `semantic_vad` 는 create_response 가 기본 true 라 **유저 턴 커밋이 응답을 자동
#   생성한다**. 그 창에서 시드가 또 쏘면 한 턴에 응답 2개가 붙었다.
#   가드는 `send_text_turn` 한 곳 — 시드 호출부 6곳이 전부 이 통로를 지난다.
@pytest.mark.asyncio
async def test_seed_is_dropped_while_a_response_is_active():
    out, ws, sess = await _drain([
        {"type": "response.created"},
        {"type": "response.output_audio_transcript.delta", "delta": "안녕"},
    ])
    before = len(ws.sent)
    await sess.send_text_turn("[시스템] 더 할 말 있니?")
    assert ws.sent[before:] == [], \
        "응답 진행 중에 시드를 보냈다 — 응답이 2개가 되거나 다음 턴에 낭독된다"


@pytest.mark.asyncio
async def test_response_done_clears_the_guard_so_seeds_work_again():
    out, ws, sess = await _drain([
        {"type": "response.created"},
        {"type": "response.done", "response": {}},
    ])
    before = len(ws.sent)
    await sess.send_text_turn("[시스템] 이제 작별 인사를 해라")
    kinds = [m["type"] for m in ws.sent[before:]]
    assert "conversation.item.create" in kinds and "response.create" in kinds, \
        "해제 뒤에는 시드가 정상으로 돌아야 한다 — 안 그러면 종료 시드가 죽어 통화가 안 끝난다"


@pytest.mark.asyncio
async def test_fresh_session_sends_the_opening_seed():
    ws = _FakeWS([])
    sess = oa.OpenAIRealtimeSession(ws)
    await sess.send_text_turn("선톡")
    assert "response.create" in [m["type"] for m in ws.sent], \
        "가드가 기본으로 켜져 있으면 선톡이 안 나가 통화가 안 열린다"


def test_turn_detection_disables_vendor_barge_in():
    """⭐ 끼어들기 없음(2026-10-07 사장님 지시) — 벤더 기본값이 `true` 라 **명시**해야 한다.

    이 통화는 barge-in off 설계다(서버 `state.turn_id` 관문 + 클라 `_micGated`). 벤더만
    끊도록 남겨 두면 call 1764 처럼 비버 턴이 중간에 잘린다(interrupted 2건·오디오 공백
    최대 3.41s·문장 절단). 세 관문의 전제를 하나로 맞춘다.
    """
    cfg = oa.build_session_config(system_instruction="x")
    td = cfg["session"]["audio"]["input"]["turn_detection"]
    assert td["type"] == "semantic_vad"
    assert td["interrupt_response"] is False, \
        "벤더 끼어들기가 켜져 있다 — barge-in off 설계와 어긋난다"
    assert "create_response" not in td, \
        "응답 생성 주체는 안 바꾼다 — false 로 두면 커밋을 놓칠 때 통화가 벙어리가 된다"
