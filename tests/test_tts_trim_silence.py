"""구간 앞뒤 침묵 잘라내기 — **속도의 큰 몫이 여기 있었다**(2026-08-10).

목표는 Live 실측 **7.7자per초**(사장님이 "속도가 딱 좋다"고 하신 실물에서 나온 값).
느린 이유는 둘로 갈렸다:
    ① 목소리 자체가 ~1.3배 느리다   Chirp en 14.4자per초 vs Gemini en 11.1
    ② **구간이 잘게 쪼개지면 침묵이 폭발한다**
         Gemini en 57자/7.3초 = 7.8   ← 같은 엔진인데 11.1 → 7.8
         Gemini ko  3자/1.5초 = 2.0   ← 3글자에 1.5초
         Chirp  ko  6자/1.1초 = 5.3   ← 같은 조건에서 Chirp 은 견딘다
어학 대화는 언어가 계속 바뀌어 구간이 잘게 쪼개지므로 **구간 수만큼 침묵이 곱해진다.**

⛔ 값(7.7)을 여기 박지 않는다 — 벤더가 바뀌면 흔들린다. **성질**로 박는다:
  ① 같은 텍스트의 오디오가 **짧아진다**
  ② **말소리는 한 샘플도 안 사라진다**(경계에서 잘리면 "무슨 말인지 모르겠다"가 된다)
  ③ **0 이 되지 않는다** — 구간 사이 틈은 남는다(다 붙이면 기계처럼 들린다)
  ④ Chirp 은 **건드리지 않는다**(지금 잘 나온다)
"""

import struct

import pytest

import core.audio as audio
import domains.learning.realtime.cascade_session as cs
from core.config import settings

RATE = audio.OUTPUT_SAMPLE_RATE


def _silence(ms: int) -> bytes:
    return b"\x00\x00" * int(RATE * ms / 1000)


def _tone(ms: int, amp: int = 12_000) -> bytes:
    """말소리 대역의 큰 신호(부호 있는 PCM16)."""
    n = int(RATE * ms / 1000)
    return b"".join(struct.pack("<h", amp if i % 2 else -amp) for i in range(n))


def _ms(pcm: bytes) -> float:
    return len(pcm) / audio.OUTPUT_BYTES_PER_MS


# ── ① 짧아진다 ─────────────────────────────────────────────────────────────
def test_edges_are_trimmed_and_audio_gets_shorter():
    """앞뒤 침묵 1초씩이 붙은 500ms 발화 → 확 짧아진다."""
    pcm = _silence(1_000) + _tone(500) + _silence(1_000)
    out = audio.trim_silence_edges(pcm, keep_head_ms=120, keep_tail_ms=120)
    assert _ms(out) < _ms(pcm) / 2, (_ms(pcm), _ms(out))
    # 남는 길이 ≈ 발화 500 + 앞뒤 여유 240
    assert 700 <= _ms(out) <= 800, _ms(out)


# ── ② 말소리는 안 사라진다 ─────────────────────────────────────────────────
def test_speech_samples_survive_completely():
    """⛔ **한 샘플도 잃지 않는다.** 잘라놓고 못 알아들으면 속도를 얻고 내용을 잃는 것이다."""
    speech = _tone(400)
    out = audio.trim_silence_edges(_silence(800) + speech + _silence(800))
    assert speech in out, "말소리 구간이 잘렸다"


def test_quiet_onset_is_protected_by_the_keep_margin():
    """⭐ 파열음(ㄱ/ㅋ/p/t)처럼 **시작이 작은 소리**가 날아가지 않는다.

    문턱을 넘은 지점에서 바로 자르면 자음이 사라진다. 그래서 소리가 시작된 곳에서
    `keep_head_ms` 만큼 **되돌아가** 자른다.
    """
    onset = _tone(60, amp=400)          # 아주 작은 시작음(문턱 아래)
    out = audio.trim_silence_edges(
        _silence(600) + onset + _tone(300), keep_head_ms=120,
    )
    assert onset in out, "작은 첫소리가 잘렸다"


# ── ③ 0 이 되지 않는다 ─────────────────────────────────────────────────────
def test_a_natural_gap_remains():
    """⛔ 구간이 딱 붙으면 기계처럼 들린다 — 우리가 고치려는 게 'AI 티'다."""
    out = audio.trim_silence_edges(_silence(500) + _tone(300) + _silence(500),
                                   keep_head_ms=120, keep_tail_ms=120)
    assert _ms(out) - 300 >= 200, "앞뒤 틈이 거의 사라졌다"


def test_all_silence_is_left_alone():
    """전부 침묵이면 **그대로 둔다** — 우리가 잘못 봤을 가능성을 남긴다."""
    pcm = _silence(500)
    assert audio.trim_silence_edges(pcm) == pcm


def test_empty_and_tiny_input_never_crashes():
    """R5 — 계측·정리가 통화를 죽이지 않는다."""
    assert audio.trim_silence_edges(b"") == b""
    assert audio.trim_silence_edges(b"\x00") == b"\x00"


def test_head_and_tail_can_be_trimmed_independently():
    """스트리밍은 **머리만**(첫 조각) / **꼬리만**(마지막 조각) 따로 다듬는다."""
    pcm = _silence(400) + _tone(200) + _silence(400)
    head_only = audio.trim_silence_edges(pcm, tail=False, keep_head_ms=100)
    tail_only = audio.trim_silence_edges(pcm, head=False, keep_tail_ms=100)
    assert _ms(head_only) < _ms(pcm) and _ms(head_only) > 500   # 꼬리는 남아 있다
    assert _ms(tail_only) < _ms(pcm) and _ms(tail_only) > 500   # 머리는 남아 있다


# ── ④ 어느 엔진에 적용하나 ─────────────────────────────────────────────────
class _Sink:
    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def receive(self):
        raise AssertionError("쓰지 않는다")


@pytest.mark.parametrize("engine,expected", [
    ("gemini-tts", True),
    ("gemini-batch", True),
    ("chirp3-hd", False),      # ⛔ 지금 잘 나온다 — 건드리지 않는다
    ("", False),               # 서버 기본값(Chirp)
])
def test_only_the_listed_engines_are_trimmed(monkeypatch, engine, expected):
    """⛔ **멀쩡한 걸 건드려 망가뜨리지 않는다.**

    Chirp 은 사장님이 "빠르게 잘 나온다"고 하신 상태이고, 실측도 같은 조건에서 Gemini 보다
    훨씬 낫다(ko 5.3 vs 2.0자per초). 적용 대상은 **이름으로 명시**한다.
    """
    session = cs.CascadeSession(_Sink())
    session._tts_engine = engine
    assert session._trim_silence() is expected


def test_keep_margin_is_a_natural_pause_not_zero():
    """남길 틈은 **사람의 절 사이 쉼**(150~250ms) 대역에 들어와야 한다(앞뒤 합).

    ⛔ 0 이면 기계처럼 들리고, 너무 크면 잘라낸 의미가 없다.
    """
    assert 50 <= settings.CASCADE_TTS_TRIM_KEEP_MS <= 200
    assert settings.CASCADE_TTS_TRIM_KEEP_MS * 2 >= 150


# ── 스트리밍 경로: **첫소리를 늦추지 않는다** ──────────────────────────────
@pytest.mark.asyncio
async def test_stream_head_trim_drops_silent_chunks_without_delaying_sound(monkeypatch):
    """⭐ 앞쪽 침묵 조각은 **버린다** — 붙들고 있는 게 침묵이라 듣는 시점은 그대로다.

    (오히려 첫 소리가 빨라진다. 지연이 생기는 쪽은 '꼬리를 붙드는 것'이라 그건 안 한다.)
    """
    session = cs.CascadeSession(_Sink())
    session._tts_engine = "gemini-tts"

    async def _src():
        yield _silence(300)          # 통째로 침묵 — 버려져야 한다
        yield _silence(200) + _tone(100)
        yield _tone(200)

    out = [chunk async for chunk in session._trim_head(_src())]
    assert len(out) == 2, "침묵 조각이 그대로 흘렀다"
    assert _ms(b"".join(out)) < 800
    assert _tone(200) in b"".join(out), "말소리가 사라졌다"


@pytest.mark.asyncio
async def test_stream_tail_trim_uses_the_chunk_already_held():
    """⭐ 꼬리는 **이미 손에 든 마지막 조각**에서 자른다 — 새 버퍼도, 추가 지연도 없다."""
    from domains.learning.realtime.cascade_reply import speak_stream

    class _Beaver:
        def __init__(self) -> None:
            self.sent = bytearray()

        async def send(self, pcm: bytes, text: str = "") -> None:
            self.sent.extend(pcm)

    async def _src():
        yield _tone(200)
        yield _tone(100) + _silence(900)      # 마지막 조각의 꼬리 침묵

    plain, trimmed = _Beaver(), _Beaver()
    await speak_stream(plain, _src(), "x", trim_tail=False)
    await speak_stream(trimmed, _src(), "x", trim_tail=True)
    assert _ms(trimmed.sent) < _ms(plain.sent), "꼬리 침묵이 그대로 나갔다"
    assert _tone(100) in bytes(trimmed.sent), "말소리 끝이 잘렸다"
