"""캐스케이드 원가 계측 — 수집·요약·로그 (크레덴셜·과금 0).

여기서 못박는 것:
  ① 벤더가 준 과금 초(total_billed_duration)를 스트림이 응답에서 실제로 걷나
  ② 요약이 **계약 모양**인가 — engine 문자열 / in_audio·out_audio = 0 / vendors 3구간
  ③ `audio_s` 에 무엇을 넣었는지 항상 `audio_s_source` 가 말하나(모르는 값을 아는 척하지 않기)
  ④ 롤오버 재생분이 별도로 세어지나(우리 카운터가 실청구보다 과소인 크기)
  ⑤ ⛔ R5 — 계측이 어떻게 망가져도 예외가 밖으로 안 나가고, 못 모았으면 **그 사실을 한 줄** 남긴다
  ⑥ 세션이 끝나면 `cascade usage:` 줄이 **반드시** 나간다(페이크 세션은 engine 이 다르게 남는다)
"""

import asyncio
import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest

import core.stt as stt_mod
from core.stt import GoogleSttV2Stream, RollingSttV2Stream
from domains.learning.realtime.cascade_session import CascadeInbound, CascadeSession
from domains.learning.realtime.cascade_usage import (
    CascadeUsage,
    format_usage_line,
    log_usage_summary,
)


# --------------------------------------------------------------------------- #
# ① 벤더 응답에서 과금 초 걷기
# --------------------------------------------------------------------------- #
def _resp(billed_s: float | None = None):
    """StreamingRecognizeResponse 흉내. proto-plus 는 Duration 을 timedelta 로 준다."""
    meta = None
    if billed_s is not None:
        meta = SimpleNamespace(total_billed_duration=timedelta(seconds=billed_s))
    return SimpleNamespace(metadata=meta, speech_event_type=None, results=())


class _StubSpeechClient:
    def __init__(self, responses):
        self._responses = responses

    async def streaming_recognize(self, requests=None):
        async def gen():
            for r in self._responses:
                yield r
        return gen()


@pytest.mark.asyncio
async def test_stt_stream_collects_billed_duration():
    """응답에 실린 과금 초를 sum·max·건수로 **셋 다** 든다.

    v2 문구('the corresponding request')만으로는 증분인지 누적 반복인지 못 정한다 —
    판정을 미루는 대신 셋을 다 들고 첫 실통화 로그로 확정한다(설계 §1-1).
    """
    stream = GoogleSttV2Stream(_StubSpeechClient([_resp(2), _resp(5), _resp(None)]), "proj", 16000)
    await stream.start()
    async for _ in stream.events():
        pass
    usage = stream.usage()
    assert usage["billed_sum_ms"] == 7000       # 2 + 5
    assert usage["billed_max_ms"] == 5000       # 누적값이 반복된 경우를 대비한 최댓값
    assert usage["billed_msgs"] == 2            # 값이 안 실린 응답은 안 센다


@pytest.mark.asyncio
async def test_stt_stream_survives_broken_metadata():
    """메타데이터가 이상해도 인식은 계속된다(R5) — 계측은 0으로 남을 뿐."""
    bad = SimpleNamespace(metadata=SimpleNamespace(total_billed_duration="이상한 값"),
                          speech_event_type=None, results=())
    stream = GoogleSttV2Stream(_StubSpeechClient([bad]), "proj", 16000)
    await stream.start()
    async for _ in stream.events():
        pass
    assert stream.usage()["billed_msgs"] == 0


# --------------------------------------------------------------------------- #
# ④ 롤오버 — 재생분·스트림 수·자식 계측 흡수
# --------------------------------------------------------------------------- #
class _Child:
    def __init__(self, billed_ms=0.0, msgs=0):
        self._u = {"billed_sum_ms": billed_ms, "billed_max_ms": billed_ms, "billed_msgs": msgs}
        self.pushed = 0

    async def push_audio(self, pcm):
        self.pushed += len(pcm)

    def usage(self):
        return dict(self._u)

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_rollover_counts_replayed_audio_separately():
    """롤오버 때 **다시 흘려 넣은** 오디오를 따로 센다.

    `_audio_ms` 는 같은 구간을 1회만 세지만 벤더는 두 스트림 모두에서 과금할 수 있다.
    없앨 수 없는 어긋남이라, **크기를 남긴다**(설계 §2-2).
    """
    rolling = RollingSttV2Stream(lambda: _Child(), 16000)
    await rolling.push_audio(b"\x00" * 3200)     # 100ms — 아직 스트림 전이라 버퍼로
    child = _Child()
    rolling._cur = child
    await rolling._flush()
    usage = rolling.usage()
    assert usage["sent_audio_ms"] == pytest.approx(100.0)
    assert usage["replay_audio_ms"] == pytest.approx(100.0)
    assert child.pushed == 3200                  # 유실 0 — 재생은 됐다


def test_absorb_usage_counts_each_stream_once():
    """같은 스트림을 두 번 걷어도 이중 계상되지 않는다(events 의 finally 와 close 가 겹친다)."""
    rolling = RollingSttV2Stream(lambda: _Child(), 16000)
    child = _Child(billed_ms=4000, msgs=2)
    rolling._absorb_usage(child)
    rolling._absorb_usage(child)
    assert rolling.usage()["billed_sum_ms"] == 4000
    assert rolling.usage()["billed_msgs"] == 2


def test_absorb_usage_tolerates_stream_without_usage():
    """usage() 가 없는 객체·터지는 객체를 만나도 세션은 계속된다(R5)."""
    rolling = RollingSttV2Stream(lambda: _Child(), 16000)

    class _Boom:
        def usage(self):
            raise RuntimeError("계측 폭발")

    rolling._absorb_usage(object())
    rolling._absorb_usage(_Boom())
    assert rolling.usage()["billed_sum_ms"] == 0


# --------------------------------------------------------------------------- #
# ②③ 요약 = 계약 모양
# --------------------------------------------------------------------------- #
class _StubStream:
    def __init__(self, **usage):
        self._u = usage

    def usage(self):
        return dict(self._u)


def _filled_usage() -> CascadeUsage:
    usage = CascadeUsage()
    usage.record_stt(
        _StubStream(streams=3, sent_audio_ms=902_400, replay_audio_ms=1_200,
                    billed_sum_ms=900_000, billed_max_ms=890_000, billed_msgs=12),
        engine="v2",
    )
    usage.record_llm(
        SimpleNamespace(prompt_token_count=41_000, candidates_token_count=3_200,
                        thoughts_token_count=120, cached_content_token_count=0,
                        total_token_count=44_320),
        vendor="gemini-2.5-flash",
    )
    usage.record_tts("  안녕하세요  ", vendor="cloud-tts-chirp3-hd")
    return usage


def test_summary_matches_contract():
    """bt-back 확정 계약: engine 문자열 / 토큰 4항 / vendors 3구간."""
    s = _filled_usage().summary(duration_s=902.4, turns=17)
    assert s["engine"] == "cascade:google-stt-v2+gemini-2.5-flash+cloud-tts-chirp3-hd"
    # 토큰 컬럼 = LLM 전용. 캐스케이드 LLM 은 오디오를 안 받는다.
    assert (s["in_text"], s["out_text"]) == (41_000, 3_200)
    assert (s["in_audio"], s["out_audio"]) == (0, 0)
    assert s["total"] == 44_320
    v = s["vendors"]
    assert set(v) == {"stt", "llm", "tts"}
    assert v["stt"]["vendor"] == "google-stt-v2" and v["stt"]["audio_s"] == 890.0
    assert v["llm"]["vendor"] == "gemini-2.5-flash"
    assert v["tts"]["vendor"] == "cloud-tts-chirp3-hd" and v["tts"]["chars"] == 5


def test_audio_s_uses_vendor_max_and_declares_its_source():
    """⭐ 실통화로 판정났다: total_billed_duration 은 **누적값이 반복해 실린다**.

    실측(통화 104.5초): billed_max=102.0s 가 실제와 맞고 billed_sum=419.0s 는 4배였다.
    → 원가 산식이 쓰는 audio_s 는 **max** 다. sum 을 썼으면 STT 원가를 4배로 과대계상했다.
    무엇을 넣었는지는 항상 audio_s_source 가 말한다.
    """
    s = _filled_usage().summary()
    stt = s["vendors"]["stt"]
    assert stt["audio_s_source"] == "vendor_billed_max"
    assert stt["audio_s"] == stt["billed_max_s"] == 890.0
    assert stt["audio_s"] != stt["billed_sum_s"]     # sum 은 중복이다
    # 원본 3종은 그대로 남는다(다음에 또 판정할 일이 생기면 이게 재료다).
    assert (stt["billed_sum_s"], stt["billed_msgs"]) == (900.0, 12)
    assert stt["sent_audio_s"] == 902.4
    assert stt["replay_audio_s"] == 1.2 and stt["streams"] == 3


def test_audio_s_falls_back_to_our_counter_when_vendor_is_silent():
    """벤더가 과금 초를 안 실어 주면(페이크·필드 미제공) 우리 카운터로 폴백하고 그렇게 밝힌다."""
    usage = CascadeUsage()
    usage.record_stt(_StubStream(sent_audio_ms=1_000, billed_msgs=0, billed_max_ms=0), engine="v2")
    stt = usage.summary()["vendors"]["stt"]
    assert stt["audio_s_source"] == "sent_audio" and stt["audio_s"] == 1.0


def test_thoughts_tokens_are_kept_out_of_out_text():
    """thoughts 는 candidates 에 없다 — 컬럼엔 안 섞고 vendors 에 남긴다(원가 산식이 더해야 한다)."""
    s = _filled_usage().summary()
    assert s["out_text"] == 3_200
    assert s["vendors"]["llm"]["thoughts"] == 120


def test_engine_lists_only_components_that_actually_ran():
    """안 돈 구간을 engine 에 적으면 원가 비교가 거짓말이 된다."""
    only_stt = CascadeUsage()
    only_stt.record_stt(_StubStream(sent_audio_ms=1000), engine="v2")
    assert only_stt.engine() == "cascade:google-stt-v2"
    # 페이크 세션(크레덴셜 없음)은 이름이 다르게 남아 실통화 원가에 섞이지 않는다.
    fake = CascadeUsage()
    fake.record_stt(_StubStream(sent_audio_ms=0), engine="fake")
    assert fake.engine() == "cascade:fake-stt"
    assert CascadeUsage().engine() == "cascade:none"


def test_tts_counts_the_string_actually_sent():
    """core/tts.py 는 strip 한 문자열을 API 에 넘긴다 → 세는 것도 strip 후 길이."""
    usage = CascadeUsage()
    usage.record_tts("\n 안녕 \n", vendor="cloud-tts-chirp3-hd")
    usage.record_tts("실패한 합성", failed=True)
    usage.record_tts_unheard(7)          # barge-in 으로 못 들려준 몫(돈은 나갔다)
    tts = usage.summary()["vendors"]["tts"]
    assert (tts["chars"], tts["calls"], tts["calls_failed"], tts["chars_unheard"]) == (2, 1, 1, 7)


# --------------------------------------------------------------------------- #
# ⑤ R5 — 망가져도 통화가 죽지 않고, 못 모았으면 그 사실을 남긴다
# --------------------------------------------------------------------------- #
def test_collection_failures_are_swallowed_and_counted():
    class _Boom:
        def usage(self):
            raise RuntimeError("계측 폭발")

    usage = CascadeUsage()
    usage.record_stt(_Boom(), engine="v2")            # 예외가 밖으로 안 나간다
    usage.record_llm(object())                        # 필드가 하나도 없는 객체
    usage.record_llm(SimpleNamespace(prompt_token_count="많이"))   # 숫자가 아닌 값
    assert usage.errors == 1                          # STT 만 실패, LLM 은 0 으로 흡수
    assert usage.summary()["in_text"] == 0


def test_missing_usage_is_logged_not_swallowed(caplog):
    """⭐ 조용히 비우지 않는다 — Live 경로가 3중으로 삼켜 아무도 모르던 그 실수를 반복하지 않는다."""
    with caplog.at_level(logging.INFO):
        assert log_usage_summary(CascadeUsage()) is None
    assert any("collected=0" in r.getMessage() for r in caplog.records)


def test_log_line_is_grepable_key_value(caplog):
    with caplog.at_level(logging.INFO):
        summary = log_usage_summary(_filled_usage(), duration_s=902.4, turns=17)
    line = format_usage_line(summary)
    fields = dict(pair.split("=", 1) for pair in line.split())
    assert fields["engine"].startswith("cascade:")
    for key in ("stt_audio_s", "stt_src", "stt_replay_s", "stt_streams", "stt_billed_sum_s",
                "llm_in", "llm_out", "llm_thoughts", "tts_chars", "tts_unheard", "err"):
        assert key in fields, key
    assert any("cascade usage:" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# ⑥ 세션 통합 — 끝나면 반드시 한 줄
# --------------------------------------------------------------------------- #
class _Transport:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.events: list[dict] = []

    async def send_event(self, event: dict) -> None:
        self.events.append(event)

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def receive(self) -> CascadeInbound:
        if self._scripted:
            return self._scripted.pop(0)
        return CascadeInbound(kind="control", control={"type": "stop"})


@pytest.mark.asyncio
async def test_session_emits_usage_line_on_close(monkeypatch, caplog):
    """세션 1건이 끝나면 원가 한 줄이 나간다 — 캐스케이드가 얼마 드는지 영영 모르지 않도록."""
    monkeypatch.setattr(stt_mod.settings, "STT_V2_FAKE", True)
    stt_mod.get_speech_v2_client.cache_clear()
    transport = _Transport([CascadeInbound(kind="control", control={"type": "start"})])
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(CascadeSession(transport).run(), timeout=5)
    stt_mod.get_speech_v2_client.cache_clear()
    line = next(r.getMessage() for r in caplog.records if "cascade usage:" in r.getMessage())
    assert "engine=cascade:fake-stt" in line     # 페이크 세션임이 로그에 드러난다
    assert "turns=0" in line


@pytest.mark.asyncio
async def test_fake_stream_counts_audio_length(monkeypatch):
    """페이크여도 **흘린 오디오 길이는** 센다 — 0.0초면 '계측 미배선'과 구분이 안 된다."""
    monkeypatch.setattr(stt_mod.settings, "STT_V2_FAKE", True)
    stt_mod.get_speech_v2_client.cache_clear()
    stream = stt_mod.make_stt_v2_stream(16000)
    await stream.push_audio(b"\x00" * 32_000)     # 1초
    stt_mod.get_speech_v2_client.cache_clear()
    usage = CascadeUsage()
    usage.record_stt(stream, engine="fake")
    stt = usage.summary()["vendors"]["stt"]
    assert stt["audio_s"] == 1.0 and stt["billed_msgs"] == 0
