"""오디오 포맷 상수 및 헬퍼 (normalcall).

입력: raw PCM 16-bit 16kHz mono (Gemini Live 로 보낼 형식).
출력: raw PCM 16-bit 24kHz mono (Gemini Live 가 돌려주는 형식).
발음평가(SpeechSuper)는 WAV 16k/16bit/mono 를 기대하므로 pcm16_to_wav 로 감싼다.
"""

from __future__ import annotations

import array
import io
import logging
import shutil
import subprocess
import wave

logger = logging.getLogger(__name__)

INPUT_SAMPLE_RATE = 16_000
OUTPUT_SAMPLE_RATE = 24_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2  # 16-bit

# ⭐ 출력 PCM 1ms 당 바이트 수. **말하기 속도(자per초)를 두 엔진이 같은 식으로 재게 하는
#   단일 출처다**(2026-08-10). Live 와 캐스케이드가 각자 계산하면 비교 자체가 무의미해진다 —
#   사장님이 "라이브 속도가 정답"이라고 하신 이상, 두 값은 같은 잣대로 나와야 한다.
OUTPUT_BYTES_PER_MS = OUTPUT_SAMPLE_RATE * SAMPLE_WIDTH_BYTES / 1000.0


def output_audio_s(audio_bytes: int) -> float:
    """출력 PCM 바이트 → 초. 음수·잡값은 0 으로 본다(계측이 통화를 죽이지 않는다)."""
    try:
        return max(0, int(audio_bytes)) / OUTPUT_BYTES_PER_MS / 1000.0
    except (TypeError, ValueError):
        return 0.0


# ── 구간 앞뒤 침묵 잘라내기 (2026-08-10) ───────────────────────────────────
# 왜: TTS 는 구간마다 앞뒤에 침묵을 붙여 준다. 어학 대화는 언어가 계속 바뀌어 구간이 잘게
# 쪼개지므로, **그 침묵이 구간 수만큼 곱해진다.** 실측(Gemini-TTS):
#     en 117자/10.5초 = 11.1자per초   ← 긴 구간(침묵 영향 작다)
#     ko   3자/1.5초  =  2.0자per초   ← 3글자에 1.5초. 말이 느린 게 아니라 침묵이 붙은 것이다
# 목표는 Live 실측 **7.7자per초**(사장님이 "속도가 딱 좋다"고 하신 실물에서 나온 값).
_TRIM_WINDOW_MS = 10          # 이 길이로 훑으며 소리가 있는지 본다
_TRIM_ABS_FLOOR = 0.005       # 정규화 RMS 절대 바닥(마이크 실측 발화 하단 0.011 보다 낮게)
_TRIM_PEAK_RATIO = 0.02       # 그 구간 최대치의 2% — 목소리·엔진마다 음량이 달라서 상대값도 본다


def _window_rms(pcm: bytes, start: int, end: int) -> float:
    """[start, end) 샘플 구간의 정규화 RMS(0~1). 파이썬 3.13 에 audioop 이 없다."""
    total = 0.0
    n = 0
    for i in range(start, end):
        lo = pcm[2 * i]
        hi = pcm[2 * i + 1]
        v = (hi << 8) | lo
        if v >= 0x8000:
            v -= 0x10000
        total += (v / 32768.0) ** 2
        n += 1
    return (total / n) ** 0.5 if n else 0.0


def upsample_16k_to_24k(pcm: bytes) -> bytes:
    """PCM16 16kHz → 24kHz. **2:3 정수비**라 부동소수 리샘플러가 필요 없다.

    왜 필요한가: OpenAI Realtime 전사가 `rate: 16000` 을 거절한다(`integer_below_min_value`).
    우리 클라는 16k 를 보낸다 — 서버에서 올려 보내는 수밖에 없다.

    방식: 입력 두 표본(a, b)마다 **a, (a+b)/2, b** 세 표본을 낸다(선형 보간).
    ⛔ 무거운 리샘플러를 넣지 않는다 — cpu=1 Cloud Run 이고 이 경로는 통화 내내 돈다.
      정수 산술뿐이라 1초치(16,000표본)에 파이썬으로도 수 ms 다.
    ⚠ 표본 수가 홀수면 마지막 한 표본은 그대로 붙인다(0.06ms — 프레임 경계에서만, 누적 없음).
    """
    if len(pcm) < 4:
        return pcm
    src = array.array("h")
    src.frombytes(pcm[: len(pcm) // 2 * 2])
    out = array.array("h")
    n = len(src) - 1
    for i in range(0, n, 2):
        a, b = src[i], src[i + 1]
        out.append(a)
        out.append((a + b) // 2)
        out.append(b)
    if len(src) % 2:
        out.append(src[-1])
    return out.tobytes()


def _loud_windows(pcm: bytes, sample_rate: int) -> tuple[list[int], int, int]:
    """(소리가 있는 창들의 인덱스, 창 크기(샘플), 전체 샘플 수).

    문턱은 **절대 바닥과 그 구간 최대치의 2% 중 큰 값**이다 — 목소리·엔진마다 음량이 달라
    절대값 하나로는 한쪽에서 과하게(또는 전혀) 안 걸린다.
    """
    samples = len(pcm) // 2
    win = max(1, int(sample_rate * _TRIM_WINDOW_MS / 1000))
    rms = [_window_rms(pcm, i, min(i + win, samples)) for i in range(0, samples, win)]
    if not rms:
        return [], win, samples
    floor = max(_TRIM_ABS_FLOOR, max(rms) * _TRIM_PEAK_RATIO)
    return [i for i, v in enumerate(rms) if v >= floor], win, samples


def align_pcm16(chunk: bytes, carry: bytes = b"") -> tuple[bytes, bytes]:
    """PCM16 을 **샘플 경계**로 자른다 → (내보낼 조각, 다음으로 이월할 나머지).

    ⛔ **1바이트를 버리면 안 된다.** PCM16 은 2바이트가 표본 하나라, 중간에서 1바이트를
      버리면 **그 뒤 전부가 1바이트 밀려** 소리가 통째로 잡음이 된다. 반드시 이월한다.
      (버려도 되는 건 스트림이 끝난 뒤 남은 1바이트뿐이다 — 0.02ms.)

    왜 필요한가(2026-08-11 실측): OpenAI TTS 는 HTTP 청크를 **받은 그대로** 흘리는데
    그 경계가 표본 중간에 떨어진다 — 조각 대부분이 **1369바이트(홀수)** 였고 회차에 따라
    홀수 비율이 51~87% 였다. Chirp(1920의 배수)·Gemini(gRPC 메시지)는 0% 다.
    홀수 조각을 받은 클라는 `new Int16Array(buf)` 에서 예외가 나 **그 조각을 통째로 버렸다** —
    28.5ms 짜리 구멍이 초당 열댓 번 뚫려 말이 잘게 씹혔다.
    """
    buf = carry + chunk if carry else chunk
    cut = len(buf) - (len(buf) % 2)
    return buf[:cut], buf[cut:]


def has_audible_signal(pcm: bytes, *, sample_rate: int = OUTPUT_SAMPLE_RATE) -> bool:
    """이 조각에 **소리가 들어 있나**(통째로 침묵인가).

    ⛔ `trim_silence_edges` 로는 이걸 알 수 없다 — 그 함수는 "전부 침묵이면 **그대로**
      돌려주는" 규약이라(멀쩡한 오디오를 지우지 않기 위해) 반환값이 비지 않는다.
      스트리밍에서 앞쪽 침묵 조각을 버리려면 판별이 따로 있어야 한다.
    """
    if len(pcm) < 2:
        return False
    loud, _, _ = _loud_windows(pcm, sample_rate)
    return bool(loud)


def trim_silence_edges(
    pcm: bytes,
    *,
    sample_rate: int = OUTPUT_SAMPLE_RATE,
    keep_head_ms: int = 120,
    keep_tail_ms: int = 120,
    head: bool = True,
    tail: bool = True,
) -> bytes:
    """구간 **앞뒤**의 침묵을 잘라내고, 자연스러운 틈(keep_*_ms)은 남긴다.

    ⛔ **낱말 첫소리를 자르지 않는다.** 소리가 시작되는 지점을 찾은 뒤 `keep_head_ms` 만큼
      **되돌아가서** 자른다 — 파열음(ㄱ/ㅋ/p/t)은 시작이 원래 작아서, 문턱을 넘은 지점에서
      바로 자르면 자음이 날아가고 "무슨 말인지 모르겠다"가 된다. 속도를 얻고 내용을 잃는 건 실패다.
    ⛔ **0 으로 만들지 않는다.** 구간 사이 틈이 사라지면 기계처럼 들린다 — 우리가 고치려는 게
      "AI 티"인데 그걸 악화시키면 본말전도다.
    ⚠ 소리가 한 군데도 없으면(전부 침묵) **그대로 돌려준다** — 우리가 잘못 봤을 가능성을
      남긴다(멀쩡한 오디오를 지우는 쪽이 훨씬 나쁘다).
    """
    if not pcm or len(pcm) < 2:
        return pcm
    loud, win, samples = _loud_windows(pcm, sample_rate)
    if not loud:
        return pcm
    start = 0
    end = samples
    if head:
        keep = int(sample_rate * max(0, keep_head_ms) / 1000)
        start = max(0, loud[0] * win - keep)
    if tail:
        keep = int(sample_rate * max(0, keep_tail_ms) / 1000)
        end = min(samples, (loud[-1] + 1) * win + keep)
    if end <= start:
        return pcm
    return pcm[start * 2 : end * 2]


# Gemini send_realtime_input 입력 오디오 MIME 타입.
INPUT_MIME_TYPE = f"audio/pcm;rate={INPUT_SAMPLE_RATE}"


def pcm16_to_wav(
    pcm: bytes,
    *,
    sample_rate: int = INPUT_SAMPLE_RATE,
    channels: int = CHANNELS,
) -> bytes:
    """raw PCM 16-bit 바이트열을 WAV(RIFF) 컨테이너로 감싼다."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


# ── MP3 인코딩 (ffmpeg) ─────────────────────────────────────────────────────
# 통화 원본/연습 녹음을 표준 MP3 로 저장해 어디서든 재생되게 한다. ffmpeg 가
# PATH 에 없거나 인코딩이 실패하면 None 을 돌려 호출부가 WAV 로 폴백한다(graceful).
_MP3_BITRATE = "128k"


def ffmpeg_available() -> bool:
    """ffmpeg 실행 파일이 PATH 에 있는지."""
    return shutil.which("ffmpeg") is not None


def _ffmpeg_to_mp3(input_bytes: bytes, input_args: list[str]) -> bytes | None:
    """ffmpeg 로 input_bytes(stdin) → MP3(stdout). 실패 시 None."""
    if not input_bytes or not ffmpeg_available():
        return None
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        *input_args, "-i", "pipe:0",
        "-ac", "1", "-b:a", _MP3_BITRATE, "-f", "mp3", "pipe:1",
    ]
    try:
        proc = subprocess.run(
            cmd, input=input_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
        logger.warning("ffmpeg MP3 인코딩 실패(rc=%s): %s", proc.returncode, proc.stderr[:200])
    except Exception as exc:  # noqa: BLE001 - 미설치/타임아웃/임의 예외 graceful
        logger.warning("ffmpeg MP3 인코딩 예외: %s", exc)
    return None


def pcm16_to_mp3(pcm: bytes, *, sample_rate: int = INPUT_SAMPLE_RATE) -> bytes | None:
    """raw PCM 16-bit(mono) → MP3 바이트. ffmpeg 없거나 실패하면 None."""
    return _ffmpeg_to_mp3(
        pcm, ["-f", "s16le", "-ar", str(sample_rate), "-ac", "1"]
    )


def wav_to_mp3(wav_bytes: bytes) -> bytes | None:
    """WAV(RIFF) 바이트 → MP3 바이트(ffmpeg 가 포맷 자동 감지). 실패 시 None."""
    return _ffmpeg_to_mp3(wav_bytes, [])
