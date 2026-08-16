"""구간 **앞뒤** 침묵을 걷어낸다 — 짧은 구간일수록 느리게 들리던 것.

## 증상과 근거
서버 로그(같은 통화)와 프론트 실측이 같은 모양을 보였다 — **짧을수록 느리다**:

    ko 39자/6.8초 = 5.7자per초   ← 긴 구간(여백이 희석된다)
    ko  9자/2.8초 = 2.5자per초   ← 짧은 구간
    프론트: "네, 완벽했어요" 7자/2.65초 = 2.6자per초

글자당 시간이 아니라 **구간당 고정비**가 붙는다는 신호다. 우리는 문장×언어로 잘게 쪼개므로
그 고정비를 **구간마다** 문다.

## 무엇이 실제로 빠져 있었나 — 머리가 아니라 **꼬리**다
머리 절단은 **원래 돌고 있었다**(`_trim_edges` 의 앞부분, `CASCADE_TTS_TRIM_ENGINES` 에
`gemini-tts` 가 들어 있다). 빠진 건 꼬리다: `speak_stream` 은 **마지막 조각 하나**만 잘랐고,
꼬리 침묵이 그 조각보다 길면 나머지는 그대로 나갔다.

## 여기서 고정하는 성질
  ① 앞쪽: 통째로 침묵인 조각은 **안 내보낸다**(첫 소리가 그만큼 빨라진다)
  ② 뒤쪽: 끝에 붙은 침묵 조각은 **여러 개여도** 걷어낸다 — `keep_tail` 만 남긴다
  ③ ⛔ **말 사이 침묵은 그대로 나간다**(뒤에 소리가 오면 붙들었던 것을 즉시 흘린다) —
     지우면 말이 뭉개지고 기계처럼 들린다
  ④ ⛔ **오디오 지연 0** — 붙드는 건 침묵뿐이다. 소리 조각은 절대 안 붙든다
  ⑤ I6: 나가는 바이트는 항상 짝수(PCM16)
  ⑥ 걷어낸 양이 **로그로 남는다** — 로컬엔 TTS 키가 없어 벤더 오디오를 못 재므로
     **실통화가 이 값을 답한다**(0 이 계속이면 절단이 안 도는 것이다)
"""

import asyncio

import pytest

import domains.learning.realtime.cascade_session as cs

_MS = int(cs.BEAVER_BYTES_PER_MS)          # 1ms 어치 바이트(PCM16/24k = 48)


def _silence(ms: int) -> bytes:
    return b"\x00\x00" * (_MS * ms // 2)


def _tone(ms: int) -> bytes:
    """또렷한 소리 — ⚠ 0 으로 채우면 그건 침묵이라 이 시험이 재려는 것에 닿지 못한다."""
    return bytes([0x00, 0x40]) * (_MS * ms // 2)


async def _run(chunks, keep_ms=120):
    session = cs.CascadeSession(object(), object())
    session.settings = cs.settings

    async def _src():
        for c in chunks:
            yield c

    report: dict = {}
    out = [c async for c in session._trim_edges(_src(), report)]
    return b"".join(out), out, report


@pytest.mark.asyncio
async def test_leading_silence_never_goes_out():
    """① 앞쪽 침묵 조각은 안 나간다 — 첫 소리가 그만큼 빨라진다."""
    got, _, report = await _run([_silence(300), _silence(200), _tone(500)])

    assert len(got) < len(_silence(500) + _tone(500)), "앞 침묵이 그대로 나갔다"
    assert report["trim_head_ms"] >= 500, report
    assert len(got) >= len(_tone(500)) * 0.9, "소리까지 잘라먹었다"


@pytest.mark.asyncio
async def test_trailing_silence_is_dropped_even_across_many_chunks():
    """⭐⭐ ② **여러 조각에 걸친** 꼬리 침묵도 걷어낸다 — 이게 빠져 있던 부분이다.

    예전 구현은 마지막 조각 하나만 잘랐다. 조각이 5개면 4개 분량이 그대로 나갔고,
    그 값이 **구간마다** 붙었다.
    """
    chunks = [_tone(400)] + [_silence(200)] * 5      # 꼬리 침묵 1,000ms
    got, _, report = await _run(chunks)

    assert report["trim_tail_ms"] >= 800, f"꼬리를 거의 못 걷어냈다: {report}"
    tail_kept = len(got) - len(_tone(400))
    assert 0 <= tail_kept <= 130 * _MS, f"남긴 틈이 과하다({tail_kept / _MS:.0f}ms)"


@pytest.mark.asyncio
async def test_a_pause_between_words_survives():
    """⛔ ③ **말 사이 침묵은 지우지 않는다.** 지우면 말이 뭉개지고 기계처럼 들린다."""
    chunks = [_tone(300), _silence(200), _tone(300)]
    got, _, report = await _run(chunks)

    assert report["trim_tail_ms"] == 0, "말 사이를 꼬리로 오해했다"
    assert len(got) >= len(_tone(600) + _silence(200)) * 0.99, (
        "가운데 쉼이 사라졌다 — 두 말이 붙어 들린다"
    )


@pytest.mark.asyncio
async def test_audio_chunks_are_never_held_back():
    """⛔ ④ **지연 0.** 소리 조각은 도착 즉시 나간다(침묵만 붙든다).

    소리를 붙들면 첫소리가 늦어진다 — 이 통화의 제일 약한 곳이라 절대 안 된다.
    """
    session = cs.CascadeSession(object(), object())
    order: list[str] = []

    async def _src():
        order.append("in:tone1")
        yield _tone(200)
        order.append("in:tone2")
        yield _tone(200)

    async for _ in session._trim_edges(_src(), {}):
        order.append("out")

    # 소리가 들어오자마자 나간다 — in/out 이 번갈아야 한다(붙들면 in 이 연속으로 쌓인다).
    assert order == ["in:tone1", "out", "in:tone2", "out"], order


@pytest.mark.asyncio
async def test_every_emitted_chunk_is_sample_aligned():
    """⑤ I6 — 나가는 바이트는 항상 짝수다(홀수면 이후 전 표본이 밀린다)."""
    chunks = [_silence(150), _tone(310), _silence(90), _tone(70), _silence(430)]
    _, out, _ = await _run(chunks)

    assert out, "아무것도 안 나갔다"
    assert all(len(c) % 2 == 0 for c in out), [len(c) for c in out]


@pytest.mark.asyncio
async def test_all_silence_produces_no_audio_but_is_reported():
    """구간이 통째로 침묵이면 낼 것이 없다 — 그래도 **얼마나 버렸는지는 남긴다**."""
    got, out, report = await _run([_silence(200)] * 4)

    assert got == b"", out
    assert report["trim_head_ms"] >= 800, report


def test_the_trimmed_amount_reaches_the_reply_log():
    """⑥ 걷어낸 양이 **요청 줄에 남는다** — 실통화가 "몇 ms 인가"에 답하게 하는 유일한 길이다.

    (로컬엔 TTS SA 키가 없어 벤더 오디오를 직접 못 잰다. 그래서 서버가 말하게 만든다.)
    """
    session = cs.CascadeSession(object(), object())
    session._reply_spans.append(("ko", 7, 48_000))
    session._tts_trims.append((320, 640))

    line = session._tts_request_log()
    assert "침묵-320/640ms" in line, line
