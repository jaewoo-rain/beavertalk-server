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

# Gemini send_realtime_input 입력 오디오 MIME 타입.
INPUT_MIME_TYPE = f"audio/pcm;rate={INPUT_SAMPLE_RATE}"


def is_valid_pcm16_frame(data: bytes) -> bool:
    """PCM 16-bit 프레임(2바이트 정렬, 비어있지 않음)인지 검증한다."""
    return len(data) > 0 and len(data) % SAMPLE_WIDTH_BYTES == 0


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
