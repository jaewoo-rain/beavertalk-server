"""발음 챌린지 서버 STT 세션 — 페이크 스트림으로 WS 계약/브리지 검증.

크레덴셜 없이(STT_FAKE) config → __test_say → final 에코 경로가 도는지 확인한다.
core.stt 는 STT_FAKE·키부재면 FakeSttStream 으로 폴백하므로 실제 과금/네트워크 0.
"""

import asyncio

import pytest

import core.stt as stt_mod
from domains.learning.realtime.stt_session import PronSttSession, SttInbound


@pytest.fixture
def fake_stt(monkeypatch):
    """settings.STT_FAKE 를 켜고 클라 캐시를 비워 make_stt_stream 이 페이크로 폴백하게 한다."""
    monkeypatch.setattr(stt_mod.settings, "STT_FAKE", True)
    stt_mod.get_speech_client.cache_clear()
    yield
    stt_mod.get_speech_client.cache_clear()


class _StubTransport:
    """스크립트된 inbound 를 순서대로 내주고, 나간 이벤트를 수집한다.

    'final' 이벤트가 나갈 때까지 stop 을 미뤄(pump_out 이 결과를 방출할 시간 확보) 레이스 제거.
    """

    def __init__(self, scripted: list[SttInbound]) -> None:
        self._scripted = list(scripted)
        self.events: list[dict] = []
        self._got_final = asyncio.Event()

    async def send_event(self, event: dict) -> None:
        self.events.append(event)
        if event.get("type") == "final":
            self._got_final.set()

    async def receive(self) -> SttInbound:
        if self._scripted:
            return self._scripted.pop(0)
        # 스크립트 소진 후: final 이 방출될 때까지 기다렸다가 stop 으로 정상 종료.
        await self._got_final.wait()
        return SttInbound(kind="control", control={"type": "stop"})


@pytest.mark.asyncio
async def test_fake_stt_echoes_test_say_as_final(fake_stt):
    transport = _StubTransport(
        [
            SttInbound(
                kind="control",
                control={"type": "config", "words": ["머리", "바다"], "sampleRate": 16000},
            ),
            SttInbound(kind="control", control={"type": "__test_say", "text": "머리"}),
        ]
    )
    await asyncio.wait_for(PronSttSession(transport).run(), timeout=5)

    types = [e.get("type") for e in transport.events]
    assert "ready" in types, f"ready 이벤트 없음: {transport.events}"
    finals = [e for e in transport.events if e.get("type") == "final"]
    assert finals and finals[0]["text"] == "머리", f"final 머리 없음: {transport.events}"


@pytest.mark.asyncio
async def test_config_missing_uses_defaults_and_still_runs(fake_stt):
    # 첫 메시지가 config 가 아니어도(오디오 먼저) 죽지 않고 ready 후 동작해야 한다.
    transport = _StubTransport(
        [
            SttInbound(kind="audio", audio=b"\x00\x00"),
            SttInbound(kind="control", control={"type": "__test_say", "text": "바다"}),
        ]
    )
    await asyncio.wait_for(PronSttSession(transport).run(), timeout=5)
    finals = [e for e in transport.events if e.get("type") == "final"]
    assert finals and finals[0]["text"] == "바다"


@pytest.mark.asyncio
async def test_immediate_disconnect_is_clean(fake_stt):
    # 첫 메시지가 disconnect 면 조용히 종료(이벤트 없음, 예외 없음).
    transport = _StubTransport([SttInbound(kind="disconnect")])
    await asyncio.wait_for(PronSttSession(transport).run(), timeout=5)
    assert transport.events == []
