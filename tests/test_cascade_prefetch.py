"""구간 **선행 합성**(pipelining) — 언어가 바뀔 때 나던 공백을 없앤다.

증상(2026-08-11 사장님): "처음 여보세요하고 **언어 바뀔 때 살짝 끊긴다**".
로그가 자리를 지목했다 — `요청=[5자·1.35s·홀수40, 90자·5.80s·홀수72]`. 한국어 구간은
**5자인데 1.0~1.35초**다(대부분 TTFB). 구간마다 벤더 왕복이 **직렬로** 붙어, 그 동안
페이서에 줄 게 없어 소리가 빈다. 묶음 텍스트는 `_flush_batch` 시점에 **이미 다 손에 있다.**

⛔ 마커 분할 끄기는 채택하지 않는다(사장님 판단) — 영어 음성이 한국어를 읽으면 학습자가
  틀린 발음을 따라 한다. 그래서 **구간별 음성은 그대로 두고** 합성 시점만 앞당긴다.

여기서 고정하는 성질:
  ① 송출 **순서**는 절대 유지된다(먼저 만들어졌다고 먼저 나가면 말이 뒤섞인다)
  ② 취소(barge-in)되면 **미리 만든 것도 같이 버린다**
  ③ I3(실시간 페이싱)·I6(짝수 바이트)가 그대로 산다
  ④ 동시 벤더 요청이 **엔진별 상한**을 안 넘는다(Gemini 는 분당 10회 — 직렬 고정)
  ⑤ 한 구간이 실패해도 **나머지 구간은 나간다**(R5)
"""

import asyncio

import pytest

import domains.learning.realtime.cascade_session as cs


class _Recorder:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        self.frames.append(frame)

    async def receive(self):
        raise AssertionError("쓰지 않는다")


def _session(monkeypatch, engine: str = "openai-tts") -> cs.CascadeSession:
    monkeypatch.setattr(cs.settings, "GPT_API_KEY", "x")
    session = cs.CascadeSession(_Recorder())
    session._tts_engine = engine
    return session


def _mark(byte: int, ms: int = 100) -> bytes:
    """구간을 알아볼 수 있는 오디오(같은 값으로 채운다)."""
    return bytes([byte]) * (48 * ms)


class _FakeVendor:
    """구간별로 **다른 지연**과 **다른 내용**을 내는 가짜 벤더."""

    def __init__(self, plan: dict[str, tuple[int, float]]) -> None:
        self.plan = plan                 # 텍스트 → (채울 바이트, 첫 조각까지 지연)
        self.open_at: list[float] = []   # 요청이 열린 시각(동시성 확인용)
        self.live = 0                    # 지금 열려 있는 요청 수
        self.peak = 0

    def stream(self, text, **kwargs):
        byte, delay = self.plan[text.strip()]
        self.live += 1
        self.peak = max(self.peak, self.live)
        self.open_at.append(asyncio.get_running_loop().time())

        async def _gen():
            try:
                await asyncio.sleep(delay)      # 벤더 왕복(TTFB)
                yield _mark(byte)
                yield _mark(byte)
            finally:
                self.live -= 1

        return _gen()


# ── ① 순서 ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_segments_go_out_in_order_even_when_a_later_one_finishes_first(monkeypatch):
    """⛔ **먼저 만들어졌다고 먼저 나가면 안 된다.** 뒤 구간을 훨씬 빨리 끝내 놓고 본다."""
    vendor = _FakeVendor({"영어다": (1, 0.20), "한국어다": (2, 0.0), "또영어": (3, 0.0)})
    monkeypatch.setattr(cs.openai_tts, "synthesize_stream", vendor.stream)
    session = _session(monkeypatch)
    await session.beaver.begin()

    sent = await session._speak_segments(
        [("영어다", "en"), ("한국어다", "ko"), ("또영어", "en")]
    )
    assert sent > 0
    order = [f[0] for f in session.transport.frames]      # 각 프레임의 첫 바이트 = 구간 표식
    assert order == sorted(order), f"구간이 뒤섞였다: {order}"
    assert set(order) == {1, 2, 3}


# ── ② 취소 ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_cancelling_drops_the_prefetched_audio_too(monkeypatch):
    """⛔ 끊었는데 **미리 만든 소리가 나중에 나오면** 안 된다(오늘 고친 그 계열)."""
    started: list[asyncio.Task] = []
    vendor = _FakeVendor({"첫구간": (1, 0.05), "둘째": (2, 0.0), "셋째": (3, 0.0)})
    monkeypatch.setattr(cs.openai_tts, "synthesize_stream", vendor.stream)
    session = _session(monkeypatch)
    await session.beaver.begin()

    task = asyncio.create_task(
        session._speak_segments([("첫구간", "en"), ("둘째", "ko"), ("셋째", "en")])
    )
    await asyncio.sleep(0)          # 선행 합성이 뜨도록 한 바퀴 돌린다
    started.append(task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)        # 남은 펌프가 있었다면 이 사이에 흘러나온다

    pending = [t for t in asyncio.all_tasks() if "pump_segment" in str(t.get_coro())]
    assert not [t for t in pending if not t.done()], "선행 합성이 살아남았다"
    assert vendor.live == 0, "벤더 스트림이 안 닫혔다"


# ── ③ I3·I6 ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_prefetched_audio_is_still_paced_and_sample_aligned(monkeypatch):
    """⛔ 미리 만들어 뒀다고 **한꺼번에 밀어내면** 클라 버퍼가 부푼다(I3).

    그리고 정렬은 구간마다 각자 걸려야 한다(I6) — 홀수를 내는 벤더로 확인한다.
    """
    class _Clock:
        def __init__(self) -> None:
            self.t, self.slept = 0.0, 0.0

        def now(self) -> float:
            return self.t

        async def sleep(self, seconds: float) -> None:
            self.slept += seconds
            self.t += seconds

    def _odd_stream(text, **kwargs):
        async def _gen():
            for _ in range(4):
                yield bytes([7]) * 1369          # 홀수 — 실제 벤더가 내는 모양
        return _gen()

    monkeypatch.setattr(cs.openai_tts, "synthesize_stream", _odd_stream)
    clock = _Clock()
    session = _session(monkeypatch)
    session.beaver = cs.BeaverOutput(session.transport, now=clock.now, sleep=clock.sleep)
    session.beaver.lead_ms = 200
    await session.beaver.begin()

    await session._speak_segments([("가", "ko"), ("나", "en"), ("다", "ko")])
    assert clock.slept > 0, "페이싱이 안 걸렸다 — 미리 만든 걸 한꺼번에 밀어냈다"
    assert not [f for f in session.transport.frames if len(f) % 2], "홀수 프레임이 나갔다"


# ── ④ 동시 요청 상한 ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_concurrent_vendor_requests_never_exceed_the_engine_limit(monkeypatch):
    """⛔ **쿼터가 상한을 정한다.** 깊이를 성질 표에서 읽어 그 수를 안 넘는지 본다."""
    plan = {t: (i + 1, 0.05) for i, t in enumerate(["가", "나", "다", "라", "마"])}
    vendor = _FakeVendor(plan)
    monkeypatch.setattr(cs.openai_tts, "synthesize_stream", vendor.stream)
    session = _session(monkeypatch)
    await session.beaver.begin()

    depth = cs._TTS_PROFILES["openai-tts"].prefetch_depth
    await session._speak_segments([(t, "ko") for t in plan])
    assert vendor.peak <= depth, f"동시 요청 {vendor.peak} > 상한 {depth}"
    assert vendor.peak > 1, "선행 합성이 아예 안 걸렸다(이 시험이 무의미해진다)"


def test_gemini_stays_serial_because_of_its_quota():
    """⛔ Gemini 는 **분당 10회** 상한이다 — 미리 열면 429 를 앞당긴다. 직렬 고정."""
    assert cs._TTS_PROFILES[cs.tts.GEMINI_ENGINE].prefetch_depth == 1
    assert cs._TTS_PROFILES[cs._GEMINI_BATCH_CHOICE].prefetch_depth == 1
    assert cs._TTS_FALLBACK_PROFILE.prefetch_depth == 1, "쿼터를 모르면 늘리지 않는다"


def test_every_choice_declares_a_prefetch_depth():
    """새 엔진이 표에서 빠지면 여기서 걸린다(같은 사고를 세 번째로 겪지 않는다)."""
    for choice in cs._TTS_CHOICES:
        assert cs._TTS_PROFILES[choice].prefetch_depth >= 1, choice


# ── ⑤ 한 구간 실패 ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_one_failing_segment_does_not_kill_the_rest(monkeypatch):
    """⛔ 한 구간이 터져도 **나머지는 나가야** 한다(R5) — 대답 전체가 죽으면 안 된다."""
    def _stream(text, **kwargs):
        async def _gen():
            if text.strip() == "터진다":
                raise RuntimeError("벤더 실패")
            yield _mark(9)
        return _gen()

    monkeypatch.setattr(cs.openai_tts, "synthesize_stream", _stream)
    session = _session(monkeypatch)
    await session.beaver.begin()

    sent = await session._speak_segments(
        [("멀쩡하다", "en"), ("터진다", "ko"), ("또멀쩡", "en")]
    )
    assert sent > 0, "실패한 구간이 나머지까지 죽였다"
    assert len(session.transport.frames) >= 2


# ── 측정: 구간 대기가 로그에 남는다 ────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_segment_wait_is_measured_and_logged(monkeypatch):
    """⛔ **재지 않고 좋아졌다고 하지 않는다.** 구간이 첫 소리를 기다린 시간을 남긴다."""
    vendor = _FakeVendor({"가": (1, 0.0), "나": (2, 0.30)})
    monkeypatch.setattr(cs.openai_tts, "synthesize_stream", vendor.stream)
    session = _session(monkeypatch)
    await session.beaver.begin()

    await session._speak_segments([("가", "ko"), ("나", "en")])
    assert len(session._tts_waits) == 2
    # 첫 구간은 기다린다(벤더 왕복). 둘째는 **앞 구간 재생 중에 미리 받아 뒀다.**
    assert session._tts_waits[1] < 0.30, session._tts_waits
    assert "대기" in session._tts_request_log() or all(
        w < 0.05 for w in session._tts_waits
    ), session._tts_request_log()


@pytest.mark.asyncio
async def test_prefetch_actually_removes_the_gap(monkeypatch):
    """⭐ **효과 자체를 성질로 박는다** — 같은 벤더 지연에서 직렬(깊이1)보다 대기가 짧다."""
    plan = {"가": (1, 0.0), "나": (2, 0.25), "다": (3, 0.25)}

    async def _measure(depth: int) -> float:
        vendor = _FakeVendor(plan)
        monkeypatch.setattr(cs.openai_tts, "synthesize_stream", vendor.stream)
        session = _session(monkeypatch)
        monkeypatch.setattr(
            cs.CascadeSession, "_profile",
            lambda self: cs._TTS_PROFILES["openai-tts"].__class__(
                **{**cs._TTS_PROFILES["openai-tts"].__dict__, "prefetch_depth": depth}
            ),
        )
        await session.beaver.begin()
        await session._speak_segments([("가", "ko"), ("나", "en"), ("다", "ko")])
        return sum(session._tts_waits[1:])          # 첫 구간 왕복은 어차피 못 숨긴다

    serial = await _measure(1)
    pipelined = await _measure(3)
    assert pipelined < serial, f"선행 합성이 공백을 못 줄였다: {pipelined} vs {serial}"
