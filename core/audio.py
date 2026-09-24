"""오디오 포맷 상수 및 헬퍼 (normalcall).

입력: raw PCM 16-bit 16kHz mono (Gemini Live 로 보낼 형식).
출력: raw PCM 16-bit 24kHz mono (Gemini Live 가 돌려주는 형식).
발음평가(SpeechSuper)는 WAV 16k/16bit/mono 를 기대하므로 pcm16_to_wav 로 감싼다.
"""

from __future__ import annotations

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


def frame_rms(pcm: bytes, *, stride: int = 8) -> float:
    """PCM16 프레임의 정규화 RMS(0~1). "지금 사람이 말하고 있나"의 게이트용.

    ⚠ **표본을 건너뛴다**(stride). 게이트 판정에 정밀도는 필요 없고, 부르는 자리가
      통화 업링크(초당 45~90프레임)라 전 샘플을 도는 비용을 못 낸다.
    ⚠ 파이썬 3.13 에서 audioop 이 제거돼 순수 파이썬으로 잰다.

    ⛔⛔ P2-4(2026-09-24, bt-back QA) — 예전엔 `cascade_session._frame_rms` 가 같은
      계산을 따로 갖고 있어 "두 곳을 같이 고쳐라"였는데, 캐스케이드 엔진 삭제로
      그 파일 자체가 없다(grep 확인 — `.py` 소스 0건). 지금은 이 함수 하나뿐이다.
    """
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    total = 0.0
    count = 0
    for i in range(0, n, stride):
        sample = int.from_bytes(pcm[i * 2 : i * 2 + 2], "little", signed=True)
        total += sample * sample
        count += 1
    if not count:
        return 0.0
    return (total / count) ** 0.5 / 32768.0


# ⛔⛔ P2-6(2026-09-24, bt-back QA) — 옛 "구간 앞뒤 침묵 잘라내기" 섹션(캐스케이드
# 스트리밍 TTS 전용)을 지웠다: `upsample_16k_to_24k`·`_loud_windows`·`_window_rms`·
# `align_pcm16`·`has_audible_signal`·`trim_silence_edges` 6개 모두 호출부 0건이었다
# (grep 확인 — 이 파일 안팎 어디서도 안 불린다. `upsample_16k_to_24k`는 OpenAI
# Realtime STT 전용이었고 `align_pcm16`은 그 스트리밍 청크 경계 정렬용, 나머지
# 넷은 캐스케이드 TTS 구간 침묵 트림 한 뭉치였다 — 전부 캐스케이드 엔진 삭제로
# 유일한 소비처를 잃었다).


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
