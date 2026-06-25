"""오디오 포맷 상수 및 헬퍼 (normalcall).

입력: raw PCM 16-bit 16kHz mono (Gemini Live 로 보낼 형식).
출력: raw PCM 16-bit 24kHz mono (Gemini Live 가 돌려주는 형식).
발음평가(SpeechSuper)는 WAV 16k/16bit/mono 를 기대하므로 pcm16_to_wav 로 감싼다.
"""

from __future__ import annotations

import io
import wave

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
