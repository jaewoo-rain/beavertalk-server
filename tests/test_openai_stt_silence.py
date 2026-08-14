"""벤더 VAD 침묵창을 **우리가 정한다** — 지금까지 노브가 코드에도 config 에도 없었다.

## 무엇을 몰랐나 (2026-08-14)
우리는 `{"type": "server_vad"}` 만 보내고 침묵창을 벤더 기본값에 맡겼다. 그 값이 얼마인지
**로그로도 알 수 없었다** — 조용한 기본값의 전형이다. 그리고 그게 우리 턴 침묵(800ms)
**앞에 통째로 얹혀** 있었다.

### 얹혀 있다는 증거 (실측 `pipeline_lag_ms` 25표본, 중앙 172ms)
`audio_end_ms` 가 **진짜 말끝**이라면, 이벤트는 침묵을 다 들은 뒤에야 나올 수 있으므로
`audio_ms − audio_end_ms ≥ 침묵창` 이어야 한다. 172ms 는 그보다 훨씬 작다.
⇒ `audio_end_ms` 는 **VAD 판정 시점**(= 말끝 + 침묵창)이고, 우리가 재는 지연은 전송 지연뿐이다.
⇒ 사용자가 실제로 기다리는 시간 = **침묵창 + 800ms**.

## 여기서 고정하는 성질
  ① 침묵창을 **명시해서** 보낸다(안 보내면 그 값을 아무도 모른다)
  ② 0 이면 예전처럼 **안 보낸다**(벤더 기본값으로 되돌아가는 길을 남긴다)
  ③ 보낸 값이 **로그에 남는다** — 이 사고의 원인이 "안 보이는 값"이었다
  ④ ⛔ `type: server_vad` 는 그대로다(끄는 게 아니라 창을 정하는 것이다)
"""

import json
import logging

import pytest

import core.openai_stt as mod


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def _session_update(ws: _FakeWS) -> dict:
    hits = [m for m in ws.sent if m.get("type") == "session.update"]
    assert len(hits) == 1, ws.sent
    return hits[0]["session"]["audio"]["input"]


async def _connect(monkeypatch, ws: _FakeWS, silence_ms: int) -> mod.OpenAiRealtimeSttStream:
    """실제 소켓 없이 `start()` 의 **설정 프레임만** 만들어 본다(과금·불안정 0)."""
    monkeypatch.setattr(mod.settings, "GPT_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(mod.settings, "OPENAI_STT_SILENCE_MS", silence_ms)

    async def _fake_connect(*a, **kw):
        return ws

    # ⚠ `start()` 안에서 `import websockets` 를 하므로 **모듈 속성**을 갈아야 한다.
    import websockets

    monkeypatch.setattr(websockets, "connect", _fake_connect)

    async def _noop() -> None:
        return None

    stream = mod.OpenAiRealtimeSttStream(16_000, ["en-US"])
    monkeypatch.setattr(stream, "_read_loop", _noop)
    await stream.start()
    if stream._reader is not None:
        stream._reader.cancel()
    return stream


@pytest.mark.asyncio
async def test_the_silence_window_is_sent_explicitly(monkeypatch):
    """⭐⭐ ① 값을 **우리가 정해서** 보낸다 — 이게 없어서 1.3초 대기의 절반을 못 봤다."""
    ws = _FakeWS()
    await _connect(monkeypatch, ws, 300)

    turn = _session_update(ws)["turn_detection"]
    assert turn["type"] == "server_vad", turn      # ④ 끄는 게 아니다
    assert turn["silence_duration_ms"] == 300, turn


@pytest.mark.asyncio
async def test_zero_falls_back_to_the_vendor_default(monkeypatch):
    """② 0 = 예전 동작(안 보낸다). 되돌아가는 길을 막지 않는다."""
    ws = _FakeWS()
    await _connect(monkeypatch, ws, 0)

    turn = _session_update(ws)["turn_detection"]
    assert turn == {"type": "server_vad"}, turn


@pytest.mark.asyncio
async def test_the_value_is_logged(monkeypatch, caplog):
    """⛔ ③ 이 사고의 원인은 **안 보이는 값**이었다. 보낸 값은 반드시 로그에 남는다."""
    ws = _FakeWS()
    with caplog.at_level(logging.INFO, logger="core.openai_stt"):
        await _connect(monkeypatch, ws, 300)

    line = [r.getMessage() for r in caplog.records if "[stt-openai] 연결" in r.getMessage()]
    assert line and "침묵창=300ms" in line[0], caplog.text


@pytest.mark.asyncio
async def test_the_default_is_not_left_to_the_vendor(monkeypatch, caplog):
    """⚠ 미지정으로 돌면 그 사실이 **로그에 그렇게 적힌다**(0 을 300 처럼 읽으면 안 된다)."""
    ws = _FakeWS()
    with caplog.at_level(logging.INFO, logger="core.openai_stt"):
        await _connect(monkeypatch, ws, 0)

    line = [r.getMessage() for r in caplog.records if "[stt-openai] 연결" in r.getMessage()]
    assert line and "벤더기본(미지정)" in line[0], caplog.text
