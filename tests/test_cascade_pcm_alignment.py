"""⛔ **불변식 I6 — 클라로 나가는 오디오 바이너리는 항상 2의 배수 바이트다.**

2026-08-11 실측으로 확인된 사고:
  · OpenAI TTS 는 HTTP 청크를 **받은 그대로** 흘렸다 — 조각 대부분이 **1369바이트(홀수)** 였고
    회차에 따라 홀수 비율이 51~87% 였다(3회는 0% — 그게 "가끔 멀쩡한 통화"의 정체다).
  · Chirp 은 29조각 중 홀수 0%(1920의 배수), Gemini 는 167조각 중 0%(gRPC 메시지 단위).
    **엔진 간 차이는 이 하나였다.**
  · 클라는 `new Int16Array(buf)` 에서 RangeError → 그 조각이 재생도, 바이트 가산도 없이
    **아무 흔적 없이 사라졌다.** 28.5ms 짜리 구멍이 초당 열댓 번 뚫려 말이 잘게 씹혔다.

여기서 고정하는 성질:
  ① 홀수 조각을 내는 어댑터를 물려도 **클라로는 한 번도 홀수가 안 나간다**
  ② 바이트가 **하나도 없어지지 않고 순서도 그대로다**(1바이트만 밀려도 소리가 통째로 망가진다)
  ③ 스트림 끝에 남는 반 표본만 버린다
"""

import pytest

from core.audio import align_pcm16
from domains.learning.realtime.cascade_reply import speak_stream
import domains.learning.realtime.cascade_session as cs


class _Recorder:
    """WS 로 실제로 나간 바이너리를 그대로 모은다(클라가 받는 것과 같다)."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []

    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        self.frames.append(frame)

    async def receive(self):
        raise AssertionError("쓰지 않는다")


def _odd_stream(chunks: list[bytes]):
    async def _gen():
        for chunk in chunks:
            yield chunk

    return _gen()


# ── ① 정렬 도구 자체 ───────────────────────────────────────────────────────
def test_align_carries_the_half_sample_instead_of_dropping_it():
    """⛔ 1바이트를 **버리면** 그 뒤 전부가 밀린다 — 반드시 이월이다."""
    out, carry = align_pcm16(b"\x01\x02\x03")
    assert out == b"\x01\x02" and carry == b"\x03"
    out2, carry2 = align_pcm16(b"\x04\x05", carry)
    assert out2 == b"\x03\x04", "이월분이 다음 조각 **앞에** 붙어야 한다"
    assert carry2 == b"\x05"
    assert out + out2 + carry2 == b"\x01\x02\x03\x04\x05"    # 아무것도 안 없어진다


def test_align_is_lossless_for_any_chunking():
    """어떤 잘림이 와도 **이어 붙이면 원본**이다(길이가 짝수면 남는 것도 없다)."""
    source = bytes(range(200))
    for step in (1, 3, 7, 13, 99):
        carry, out = b"", b""
        for i in range(0, len(source), step):
            piece, carry = align_pcm16(source[i:i + step], carry)
            out += piece
        assert out + carry == source
        assert len(out) % 2 == 0


# ── ② 송출 경로 전체 ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_no_odd_binary_ever_reaches_the_client():
    """⭐ **홀수를 내는 가짜 어댑터**를 물린다 — 클라가 받는 프레임은 전부 짝수여야 한다."""
    recorder = _Recorder()
    session = cs.CascadeSession(recorder)
    await session.beaver.begin()
    # 실제 관측값과 같은 모양: 1369바이트짜리 홀수 조각이 이어진다
    chunks = [bytes([i % 251] * 1369) for i in range(5)]
    await speak_stream(session.beaver, _odd_stream(chunks), "문장")
    assert recorder.frames, "아무것도 안 나갔다"
    odd = [len(f) for f in recorder.frames if len(f) % 2]
    assert not odd, f"홀수 프레임이 나갔다: {odd}"


@pytest.mark.asyncio
async def test_the_audio_bytes_are_preserved_in_order():
    """⛔ 정렬이 **소리를 바꾸면** 안 된다 — 1바이트만 밀려도 전부 잡음이 된다.

    스트림 끝의 반 표본(홀수 총합)만 빠진다.
    """
    recorder = _Recorder()
    session = cs.CascadeSession(recorder)
    await session.beaver.begin()
    chunks = [bytes([1, 2, 3]), bytes([4, 5]), bytes([6, 7, 8, 9])]
    await speak_stream(session.beaver, _odd_stream(chunks), "문장")
    got = b"".join(recorder.frames)
    source = b"".join(chunks)
    assert got == source[:len(got)], "바이트 순서가 어긋났다"
    assert len(source) - len(got) <= 1, "스트림 끝 반 표본 말고는 버리면 안 된다"


@pytest.mark.asyncio
async def test_send_refuses_to_pass_an_odd_frame_and_says_so(caplog):
    """마지막 방어선(I6) — 정렬 경로를 안 탄 조각이 와도 클라는 짝수만 받는다."""
    import logging

    recorder = _Recorder()
    session = cs.CascadeSession(recorder)
    await session.beaver.begin()
    with caplog.at_level(logging.WARNING):
        await session.beaver.send(b"\x01\x02\x03", "")
    assert recorder.frames == [b"\x01\x02"]
    assert any("홀수 바이트" in r.getMessage() for r in caplog.records), caplog.text


@pytest.mark.asyncio
async def test_a_single_stray_byte_never_becomes_a_frame():
    """1바이트만 오면 **아무것도 안 나간다**(빈 프레임도 안 보낸다)."""
    recorder = _Recorder()
    session = cs.CascadeSession(recorder)
    await session.beaver.begin()
    await session.beaver.send(b"\x01", "")
    assert recorder.frames == []


# ── ③ 침묵 절단과의 순서 ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_alignment_happens_before_silence_trimming(monkeypatch):
    """⛔ **정렬이 침묵 절단보다 먼저**여야 한다.

    `_trim_head` 는 침묵 조각을 **통째로 버린다**. 그 조각이 홀수면 버려진 바이트 수도
    홀수라, 남은 스트림이 **반 표본 밀린 채** 시작한다 — 그러면 이후 모든 표본이
    [앞 표본의 상위, 뒤 표본의 하위]로 잘못 짝지어져 **소리 전체가 잡음**이 된다.
    ⚠ 홀수 여부만 봐서는 이걸 못 잡는다(밀려도 길이는 짝수다). **바이트 위치**를 본다.
    """
    recorder = _Recorder()
    # ⚠ 절단이 켜진 엔진을 **명시**한다(서버 기본값에 딸려가면 전제가 소리 없이 바뀐다).
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_TRIM_ENGINES", "chirp3-hd")
    silence = bytes(3001)                        # ⭐ 홀수 침묵 — 절단이 통째로 버릴 조각
    payload = [bytes([(i + 1) * 40] * 1369) for i in range(3)]   # 실제 관측과 같은 홀수 조각

    async def _stream(text, **kwargs):
        yield silence
        for chunk in payload:
            yield chunk

    monkeypatch.setattr(cs.tts, "synthesize_stream",
                        lambda *a, **kw: _wrap(_stream(*a, **kw)))
    session = cs.CascadeSession(recorder)
    session._tts_engine = "chirp3-hd"
    await session.beaver.begin()
    assert session._trim_silence() is True, "이 시험의 전제(절단 켜짐)가 깨졌다"
    await session._speak_one("문장", "ko")

    got = b"".join(recorder.frames)
    assert not [len(f) for f in recorder.frames if len(f) % 2]
    # 벤더 기준 3000번째 바이트(=표본 경계)부터 나가야 한다. 그 앞 침묵 3000바이트만 버린다.
    assert got == silence[3000:] + b"".join(payload), (
        "표본 격자가 밀렸다 — 침묵을 홀수로 버렸다는 뜻이다"
    )


async def _wrap(gen):
    """`tts.synthesize_stream` 은 **await 하면 제너레이터**를 준다 — 그 모양을 맞춘다."""
    return gen


# ── ④ 계측: 홀수가 왔다는 사실이 로그에 남는다 ─────────────────────────────
@pytest.mark.asyncio
async def test_odd_chunks_are_counted_for_the_reply_log(monkeypatch):
    """⭐ **조용한 실패를 없앤다** — 벤더가 홀수를 내면 그 숫자가 대답 줄에 남아야 한다.

    이 사고를 하루 넘게 못 찾은 이유가 "아무 흔적이 없었다"이다. 고친 뒤에도 벤더가
    여전히 홀수를 내는지는 **로그로만** 알 수 있다.
    """
    async def _stream(text, **kwargs):
        for i in range(3):
            yield bytes([(i + 1) * 40] * 1369)      # 전부 홀수

    monkeypatch.setattr(cs.tts, "synthesize_stream",
                        lambda *a, **kw: _wrap(_stream(*a, **kw)))
    session = cs.CascadeSession(_Recorder())
    await session.beaver.begin()
    await session._speak_one("문장", "ko")
    assert session._tts_odd_chunks == [3], session._tts_odd_chunks
    assert "홀수3" in session._tts_request_log(), session._tts_request_log()


@pytest.mark.asyncio
async def test_normal_engines_leave_no_noise_in_the_log(monkeypatch):
    """짝수만 오면 **아무 표시도 안 붙는다**(정상이 시끄러우면 이상을 못 본다)."""
    async def _stream(text, **kwargs):
        yield bytes(1920)

    monkeypatch.setattr(cs.tts, "synthesize_stream",
                        lambda *a, **kw: _wrap(_stream(*a, **kw)))
    session = cs.CascadeSession(_Recorder())
    await session.beaver.begin()
    await session._speak_one("문장", "ko")
    assert "홀수" not in session._tts_request_log()


# ── ⑤ 잘린 응답: 완결성이 로그에 드러난다 ──────────────────────────────────
@pytest.mark.asyncio
async def test_a_truncated_vendor_response_is_reported(monkeypatch, caplog):
    """⛔ status 200 인데 오디오만 짧게 오고 끝나는 회차가 있었다(재현 안 됨).

    어댑터에 완결성 검사가 없어 **로그에 아무것도 안 남았다.** 물리적으로 불가능한
    자/초가 나오면 경고로 드러낸다 — 속도가 아니라 절단의 신호다.
    """
    import logging

    async def _stream(text, **kwargs):
        # ⚠ **0 으로 채우면 안 된다** — 서버 기본(Gemini)은 침묵 절단이 켜져 있어 통째로
        #   버려지고, 그러면 이 시험이 재려는 것(잘린 응답 경고)에 닿지도 못한다.
        yield bytes([40]) * 14400                   # 0.30초 — 100자짜리 문장에 비해 불가능

    monkeypatch.setattr(cs.tts, "synthesize_stream",
                        lambda *a, **kw: _wrap(_stream(*a, **kw)))
    session = cs.CascadeSession(_Recorder())
    await session.beaver.begin()
    with caplog.at_level(logging.WARNING):
        await session._speak_one("가" * 100, "ko")
    assert any("너무 짧다" in r.getMessage() for r in caplog.records), caplog.text


@pytest.mark.asyncio
async def test_a_normal_response_does_not_warn(monkeypatch, caplog):
    """⚠ 정상 발화가 걸리면 경고가 무의미해진다 — 문턱은 불가능한 선이어야 한다."""
    import logging

    async def _stream(text, **kwargs):
        yield bytes([40]) * (48000 * 2)             # 2초 — 20자면 10자/초(정상)

    monkeypatch.setattr(cs.tts, "synthesize_stream",
                        lambda *a, **kw: _wrap(_stream(*a, **kw)))
    session = cs.CascadeSession(_Recorder())
    await session.beaver.begin()
    with caplog.at_level(logging.WARNING):
        await session._speak_one("가" * 20, "ko")
    assert not [r for r in caplog.records if "너무 짧다" in r.getMessage()], caplog.text


# ── ⑥ 클라가 세어 보내는 홀수 프레임 — I6 의 **유일한 외부 증인** ─────────
@pytest.mark.asyncio
async def test_the_client_reported_odd_frames_are_logged(caplog):
    """⛔ 우리가 짝수를 보장하는데 클라가 홀수를 셌다면 **그 사이에서 정렬이 깨진 것**이다.

    클라 큐는 홀수가 와도 이어붙어 재생이 안 깨진다 — **자연 신호가 없다.** 그래서 클라가
    세어 보내고, 서버는 그걸 보고 경고한다. 이 숫자가 없으면 소리만 조금씩 상한 채 아무도 모른다.
    """
    import logging

    session = cs.CascadeSession(_Recorder())
    turn_id = await session.beaver.begin()
    with caplog.at_level(logging.WARNING):
        await session._on_playback_progress({
            "type": "playback_progress", "turn_id": turn_id,
            "played_server_bytes": 0, "odd_frames": 3,
        })
    assert any("홀수 길이 오디오 프레임 3개" in r.getMessage() for r in caplog.records), caplog.text


@pytest.mark.asyncio
async def test_a_client_without_the_field_is_silent(caplog):
    """⚠ 구버전 클라는 안 보낸다 — 기본 0 이고 **조용히 넘어간다**(R5)."""
    import logging

    session = cs.CascadeSession(_Recorder())
    turn_id = await session.beaver.begin()
    with caplog.at_level(logging.WARNING):
        await session._on_playback_progress({
            "type": "playback_progress", "turn_id": turn_id, "played_server_bytes": 0,
        })
    assert not [r for r in caplog.records if "홀수 길이" in r.getMessage()]


@pytest.mark.asyncio
async def test_the_odd_frame_warning_fires_once_per_call(caplog):
    """⚠ 진행도는 턴마다 온다 — 매번 찍으면 로그가 도배된다."""
    import logging

    session = cs.CascadeSession(_Recorder())
    turn_id = await session.beaver.begin()
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            await session._on_playback_progress({
                "type": "playback_progress", "turn_id": turn_id,
                "played_server_bytes": 0, "odd_frames": 5,
            })
    assert len([r for r in caplog.records if "홀수 길이" in r.getMessage()]) == 1
