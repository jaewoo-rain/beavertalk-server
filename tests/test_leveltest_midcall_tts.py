"""PM-DEC-977 — 레벨테스트만 통화 중 0B 턴 TTS 대체 · 종료 컷 턴 제외 · 대체 재생 중 늦은 비버 턴(이중 발화) 차단 · 숙제 자료 재시도."""
import asyncio

import httpx
import pytest

from core import b2b_client
from domains.learning.realtime import call_session as cs
from tests.test_leveltest_end_cut import _WS, _ev, _state


@pytest.fixture
def fake_tts(monkeypatch, tmp_path):
    key = tmp_path / "k.json"; key.write_text("{}")
    monkeypatch.setattr(cs._settings, "TTS_SA_KEY_FILE", str(key))
    monkeypatch.setattr(cs, "_greeting_tts_sync", lambda text, voice, timeout=None: b"\x00\x00" * 24000)   # 1초
    monkeypatch.setattr(cs, "MIDCALL_TTS", False)            # 운영 LIVE_MIDCALL_TTS=0
    monkeypatch.setattr(cs, "LEVELTEST_MIDCALL_TTS", True)


def _lt():
    st = _state()
    st.tts_voice = "Fenrir"
    return st


@pytest.mark.asyncio
async def test_leveltest_mute_turn_is_voiced_even_when_global_off(fake_tts):
    st, ws = _lt(), _WS()
    assert await cs._midcall_tts_fallback(ws, st, "주말에 뭐 했어요?")
    assert ws.bytes and st.midcall_tts_count == 1 and st.midcall_echo_until > 0


@pytest.mark.asyncio
async def test_other_courses_stay_off(fake_tts):
    st, ws = _lt(), _WS()
    st.is_leveltest = False
    assert not await cs._midcall_tts_fallback(ws, st, "주말에 뭐 했어요?")
    assert ws.bytes == []


@pytest.mark.asyncio
async def test_cut_question_turn_not_revived(fake_tts):
    st, ws = _lt(), _WS()
    st.leveltest_cut_turn = "q1"           # 종료 판정으로 끊은 질문(작별 시드 전)
    assert not await cs._midcall_tts_fallback(ws, st, "한국에서 제일 가보고 싶은 데가")
    st.close_seed_sent = True              # 작별 턴이 0B 면 대체한다
    assert await cs._midcall_tts_fallback(ws, st, "Bye!")


@pytest.mark.asyncio
async def test_late_beaver_turn_during_tts_playback_is_dropped(fake_tts):
    st, ws = _lt(), _WS()
    await cs._midcall_tts_fallback(ws, st, "주말에 뭐 했어요?")
    n_bytes, n_json = len(ws.bytes), len(ws.json)
    st.user_turn_open = False
    started = await cs._forward_event(ws, _ev("audio", audio=b"\x01\x02" * 100), st)
    await cs._forward_event(ws, _ev("out_tr", text="주말에 뭐 했어요?"), st)
    await cs._forward_event(ws, _ev("turn_end"), st)
    assert not started and len(ws.bytes) == n_bytes and len(ws.json) == n_json
    assert st.turn_id is None and st.midcall_echo_turn is None and not st.cur_beaver_pcm


@pytest.mark.asyncio
async def test_reply_after_learner_speaks_is_not_dropped(fake_tts):
    st, ws = _lt(), _WS()
    await cs._midcall_tts_fallback(ws, st, "주말에 뭐 했어요?")
    st.user_turn_open = True               # 학습자가 말했다 → 비버 답은 정상
    n = len(ws.bytes)
    started = await cs._forward_event(ws, _ev("audio", audio=b"\x01\x02" * 100), st)
    assert started and len(ws.bytes) > n


@pytest.mark.asyncio
async def test_goodbye_during_guard_is_not_dropped(fake_tts):
    st, ws = _lt(), _WS()
    await cs._midcall_tts_fallback(ws, st, "주말에 뭐 했어요?")
    st.user_turn_open = False
    st.close_seed_sent = True
    assert await cs._forward_event(ws, _ev("audio", audio=b"\x01\x02" * 100), st)


@pytest.mark.asyncio
async def test_guard_expires(fake_tts):
    st, ws = _lt(), _WS()
    await cs._midcall_tts_fallback(ws, st, "주말에 뭐 했어요?")
    st.midcall_echo_until = asyncio.get_running_loop().time() - 0.1
    st.user_turn_open = False
    assert await cs._forward_event(ws, _ev("audio", audio=b"\x01\x02" * 100), st)


class _Resp:
    def __init__(self, code, body=None):
        self.status_code = code
        self._b = body or {}

    def json(self):
        return self._b


def _client_seq(seq, calls):
    class C:
        def __init__(self, timeout=None):
            calls.append(timeout)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            r = seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
    return C


def test_materials_retries_once_then_succeeds(monkeypatch):
    calls = []
    monkeypatch.setattr(b2b_client.settings, "B2B_API_BASE_URL", "https://b2b.example")
    monkeypatch.setattr(b2b_client.settings, "B2B_SERVICE_TOKEN", "t")
    monkeypatch.setattr(b2b_client.httpx, "Client", _client_seq([httpx.ConnectTimeout("cold"), _Resp(503)], calls))
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(b2b_client.HomeworkMaterialsError) as e:
        b2b_client.conversation_materials(1, assignment_id=34, locale="en")
    assert e.value.code == "homework_service_unavailable" and len(calls) == 2
    assert calls[0] is b2b_client._MATERIALS_TIMEOUT


def test_materials_cold_then_ok_goes_on(monkeypatch):
    calls = []
    monkeypatch.setattr(b2b_client.settings, "B2B_API_BASE_URL", "https://b2b.example")
    monkeypatch.setattr(b2b_client.settings, "B2B_SERVICE_TOKEN", "t")
    monkeypatch.setattr(b2b_client.httpx, "Client", _client_seq([httpx.ConnectTimeout("cold"), _Resp(404, {"detail": "assignment_not_found"})], calls))
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(b2b_client.HomeworkMaterialsError) as e:   # 두 번째 응답(4xx)까지 갔다 = 재시도가 돌았다
        b2b_client.conversation_materials(1, assignment_id=34, locale="en")
    assert len(calls) == 2 and e.value.code != "homework_service_unavailable"
