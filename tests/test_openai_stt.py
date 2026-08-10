"""OpenAI Realtime 전사 어댑터 — **세션은 이 벤더를 몰라야 한다**(2026-08-10).

붙이는 이유(실측, 같은 오디오·같은 실시간 경로 — 선정기준 §4-2):
    `안녕하세요. + <상대언어> + 돈까스가 좋아요.` 6개 언어쌍에서
      Google long+[ko,X] 1/6 · Google chirp+[ko] 2/6 · ElevenLabs 실시간 2/6
      **OpenAI Realtime 6/6**
    ⭐ 가운데 구간만 떼어 언어를 지정하면 구글도 6/6 이다 ⇒ 오디오가 아니라 **code-switching**
      문제이고 지금은 OpenAI 만 푼다. 원가도 구글의 1/5.3.

여기서 고정하는 성질:
  ① 벤더 이벤트가 **`SttV2Event` 4종으로만** 나온다(모르는 타입은 조용히 버린다)
  ② 부분 전사는 **누적 전체**다(증분을 그대로 넘기면 자막이 한 글자씩 덮인다)
  ③ 키가 없으면 **Google 로 폴백 + 경고**(조용한 폴백 금지 — 원가 벤더도 실제 엔진으로)
  ④ 16k → 24k 업샘플이 길이·샘플레이트를 정확히 바꾼다
  ⑤ 엔진 **기본값은 여전히 google**(실통화 검증 전에 기본을 바꾸지 않는다)
⛔ 실 API 를 부르지 않는다(과금·불안정). 계약만 페이크로 고정한다.
"""

import array
import struct

import pytest

import core.audio as audio
import core.openai_stt as oai
import core.stt as stt_mod
from core.stt import SPEECH_BEGIN, SPEECH_END, STREAM_ERROR, TRANSCRIPT


def _stream() -> oai.OpenAiRealtimeSttStream:
    return oai.OpenAiRealtimeSttStream(16_000, ["ko-KR", "en-US"])


# ── ① 4종으로만 나온다 ─────────────────────────────────────────────────────
def test_vendor_events_map_to_our_four_kinds():
    s = _stream()
    begin = s._translate({"type": "input_audio_buffer.speech_started", "audio_start_ms": 120})
    stop = s._translate({"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 980})
    assert [e.kind for e in begin] == [SPEECH_BEGIN] and begin[0].offset_ms == 120
    assert [e.kind for e in stop] == [SPEECH_END] and stop[0].offset_ms == 980


def test_unknown_event_types_are_dropped_silently():
    """⛔ 계약을 넓히지 않는다 — 세션이 모르는 이벤트가 새어 나가면 안 된다."""
    s = _stream()
    for kind in ("session.created", "conversation.item.created", "response.done", ""):
        assert s._translate({"type": kind}) == []


def test_errors_surface_as_stream_error():
    s = _stream()
    out = s._translate({"type": "error", "error": {"message": "boom"}})
    assert [e.kind for e in out] == [STREAM_ERROR] and "boom" in out[0].detail


# ── ② 부분 전사는 누적 전체 ────────────────────────────────────────────────
def test_deltas_accumulate_into_a_full_partial():
    """⚠ 벤더의 `delta` 는 **증분**이다. 우리 계약의 부분 전사는 '지금까지 전체'다."""
    s = _stream()
    base = {"type": "conversation.item.input_audio_transcription.delta", "item_id": "i1"}
    texts = []
    for piece in ("안녕", "하세", "요"):
        texts += [e.text for e in s._translate({**base, "delta": piece})]
    assert texts == ["안녕", "안녕하세", "안녕하세요"]

    done = s._translate({"type": "conversation.item.input_audio_transcription.completed",
                         "item_id": "i1", "transcript": "안녕하세요"})
    assert len(done) == 1 and done[0].is_final and done[0].text == "안녕하세요"
    # 최종 뒤에는 그 item 의 누적이 남지 않는다(다음 발화가 이어 붙으면 자막이 무너진다)
    assert s._partial == {}


def test_two_items_do_not_bleed_into_each_other():
    s = _stream()
    base = {"type": "conversation.item.input_audio_transcription.delta"}
    s._translate({**base, "item_id": "a", "delta": "가"})
    out = s._translate({**base, "item_id": "b", "delta": "나"})
    assert out[0].text == "나"


# ── ③ 폴백 ─────────────────────────────────────────────────────────────────
def test_missing_key_falls_back_to_google(monkeypatch, caplog):
    """⛔ 조용한 폴백 금지 — 어느 엔진이 돌았는지 모르면 실측이 거짓말이 된다."""
    caplog.set_level("WARNING")
    monkeypatch.setattr(stt_mod.settings, "CASCADE_STT_ENGINE", "openai")
    monkeypatch.setattr(stt_mod.settings, "GPT_API_KEY", "")
    monkeypatch.setattr(stt_mod.settings, "STT_V2_FAKE", True)
    stt_mod.get_speech_v2_client.cache_clear()
    stream = stt_mod.make_stt_v2_stream(16_000, ["ko-KR"])
    assert not isinstance(stream, oai.OpenAiRealtimeSttStream)
    assert any("google" in r.getMessage() for r in caplog.records), caplog.text


@pytest.mark.asyncio
async def test_connect_failure_switches_to_google_and_says_so(caplog):
    """개시가 실패하면 갈아탄다 — 그리고 **원가 벤더가 실제 엔진**을 가리킨다."""
    caplog.set_level("WARNING")

    class _Dead:
        vendor = "openai-x"

        async def start(self):
            raise RuntimeError("connect refused")

        async def close(self):
            return None

    class _Google:
        vendor = "google-stt-v2"

        async def start(self):
            return None

        async def events(self):
            return
            yield

        async def close(self):
            return None

        def usage(self):
            return {"streams": 1}

    stream = stt_mod.FallbackSttStream(_Dead, _Google, "openai")
    await stream.start()
    assert stream.vendor == "google-stt-v2", "폴백했는데 원가는 openai 로 남는다"
    assert any("폴백" in r.getMessage() for r in caplog.records), caplog.text
    assert stream.usage() == {"streams": 1}


def test_usage_reports_what_we_sent_not_a_vendor_number():
    """벤더가 과금 초를 안 준다 — 요약이 `sent_audio` 출처로 표시하게 둔다."""
    s = _stream()
    s._sent_ms = 12_345.0
    usage = s.usage()
    assert usage["sent_audio_ms"] == 12_345.0
    assert usage["billed_msgs"] == 0, "벤더 값이 있는 것처럼 보이면 안 된다"


# ── ④ 업샘플 ───────────────────────────────────────────────────────────────
def test_upsample_changes_length_by_exactly_three_halves():
    """16k → 24k = **2:3 정수비**. 부동소수 리샘플러가 필요 없다."""
    src = array.array("h", [1000, 2000, 3000, 4000]).tobytes()   # 4 표본
    out = audio.upsample_16k_to_24k(src)
    got = array.array("h")
    got.frombytes(out)
    assert len(got) == 6, got
    assert list(got) == [1000, 1500, 2000, 3000, 3500, 4000]


def test_upsample_keeps_one_second_at_one_second():
    """길이가 늘면 **말이 느려진다** — 비율이 정확해야 한다."""
    one_sec_16k = b"\x00\x00" * 16_000
    out = audio.upsample_16k_to_24k(one_sec_16k)
    assert len(out) == 24_000 * 2


def test_upsample_is_cheap_enough_for_a_call(monkeypatch):
    """⛔ cpu=1 Cloud Run 이고 이 경로는 통화 내내 돈다 — 무거우면 못 쓴다."""
    import time

    one_sec = b"".join(struct.pack("<h", (i % 1000) - 500) for i in range(16_000))
    t0 = time.perf_counter()
    audio.upsample_16k_to_24k(one_sec)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.25, f"1초 오디오에 {elapsed*1000:.0f}ms — 실시간 예산을 먹는다"


def test_upsample_survives_tiny_and_odd_input():
    assert audio.upsample_16k_to_24k(b"") == b""
    assert audio.upsample_16k_to_24k(b"\x01\x00") == b"\x01\x00"
    odd = array.array("h", [100, 200, 300]).tobytes()
    got = array.array("h")
    got.frombytes(audio.upsample_16k_to_24k(odd))
    assert list(got) == [100, 150, 200, 300]


# ── ⑤ 기본값은 google ──────────────────────────────────────────────────────
def test_engine_default_is_openai_with_google_as_the_safety_net():
    """⭐ 2026-08-10 사장님 지시로 **기본이 openai 로 뒤집혔다**(실측 6/6 vs 구글 1~2/6).

    ⚠ 예전 성질("기본은 google")을 **지운 게 아니라 뒤집어 다시 박았다** — 기본이 무엇인지는
      계속 못박혀 있어야 누가 조용히 바꿔도 잡힌다.
    ⛔ 폴백은 그대로 산다: 키가 없으면 google 로 돌고 WARNING 이 남는다(위 테스트가 지킨다).
    """
    from core.config import settings as live

    assert live.CASCADE_STT_ENGINE == "openai"
    assert live.OPENAI_STT_MODEL == "gpt-4o-mini-transcribe"


def test_single_language_is_passed_but_multi_is_left_to_auto_detect():
    """⚠ OpenAI 는 `language` 를 **하나만** 받는다 — 다국어는 '안 넣기'가 유일한 방법이다."""
    assert oai.OpenAiRealtimeSttStream(16_000, ["ko-KR"])._language == "ko-KR"
    assert oai.OpenAiRealtimeSttStream(16_000, ["ko-KR", "en-US"])._language is None
    assert oai.OpenAiRealtimeSttStream(16_000, [])._language is None


# ── ③ 끝 잘림 — 실측으로 원인이 확정됐다 ──────────────────────────────────
@pytest.mark.asyncio
async def test_close_flushes_tail_silence_so_the_last_utterance_survives(monkeypatch):
    """⭐ 실측(2026-08-10): 그냥 끊으면 전사가 2건인데 **꼬리 무음 1.5초**를 붙이면 3건째
    `돈가스가 좋아요.` 가 온다. server VAD 가 발화 끝을 못 봐서 마지막 구간을 커밋하지 않는다.

    ⚠ 통화 중에는 마이크가 상시 열려 무음이 계속 흐르므로 문제가 안 된다 — **통화 끝**에서만
    생긴다. 그래서 닫기 직전에만 메운다.
    """
    import json

    sent: list[dict] = []

    class _Ws:
        async def send(self, raw: str) -> None:
            sent.append(json.loads(raw))

        async def close(self) -> None:
            return None

    monkeypatch.setattr(oai.settings, "OPENAI_STT_TAIL_SILENCE_MS", 40)
    s = _stream()
    s._ws = _Ws()
    await s.close()
    appends = [m for m in sent if m.get("type") == "input_audio_buffer.append"]
    assert len(appends) == 1, sent
    import base64 as b64
    pcm = b64.b64decode(appends[0]["audio"])
    assert pcm == b"\x00\x00" * int(24_000 * 40 / 1000), "무음 길이가 설정과 다르다"


def test_tail_silence_default_is_long_enough_to_commit():
    """⛔ 0 으로 두면 마지막 발화가 사라진다 — 실측에서 1.5초가 통했다."""
    from core.config import settings as live

    assert live.OPENAI_STT_TAIL_SILENCE_MS >= 1000
