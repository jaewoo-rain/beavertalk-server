"""표현 TTS 합성 — Vertex AI Gemini-TTS(gemini-2.5-flash-tts, 한국어) graceful 어댑터.

통화후 분석이 배운 표현마다 호출해 한국어 오디오를 만든다. 이미 떠 있는 Vertex
genai 클라이언트(app.state.genai_client)를 그대로 재사용한다 — 별도 Cloud
Text-to-Speech API 활성화가 필요 없다(aiplatform 권한만으로 동작). import/인증/
비활성/임의 예외를 모두 흡수해 None 을 반환한다(speechsuper.py 와 동일 규율) —
TTS 가 안 돼도 분석 흐름(추출/번역/요약/저장)은 죽지 않는다.

출력: gemini-tts 는 헤더 없는 raw PCM s16le/24kHz/mono 를 준다. ffmpeg 가 있으면
MP3 로, 없으면 WAV 로 감싸 (audio_bytes, content_type) 을 돌려준다. 호출부는
content_type 으로 업로드 확장자를 정한다.
"""

from __future__ import annotations

import logging
from typing import Any

from core import audio as audio_mod
from core.config import settings

logger = logging.getLogger(__name__)

# gemini-tts 출력 샘플레이트(고정). prebuilt 음성명(ko 지원).
_TTS_SAMPLE_RATE = 24_000
_VOICE_NAME = "Charon"
_LANGUAGE_CODE = "ko-KR"


async def synthesize_korean(text: str, client: "Any | None") -> tuple[bytes, str] | None:
    """한국어 텍스트를 Vertex Gemini-TTS 로 합성 → (audio_bytes, content_type) 또는 None.

    client 는 lifespan 이 만든 genai.Client(Vertex). None 이거나 비어있는 입력/합성
    실패면 None — 호출부는 None 이면 TTS 를 건너뛴다. MP3(ffmpeg 가능 시) 우선,
    아니면 WAV 로 폴백한다.
    """
    if not text or not text.strip() or client is None:
        return None

    try:
        from google.genai import types

        resp = await client.aio.models.generate_content(
            model=settings.TTS_MODEL,
            contents=text.strip(),
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    language_code=_LANGUAGE_CODE,
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=_VOICE_NAME
                        ),
                    ),
                ),
            ),
        )
        pcm = _extract_pcm(resp)
        if not pcm:
            logger.warning("tts: 합성 결과 비어있음 → None.")
            return None

        mp3 = audio_mod.pcm16_to_mp3(pcm, sample_rate=_TTS_SAMPLE_RATE)
        if mp3:
            logger.info("tts: 합성 성공 MP3(%d bytes).", len(mp3))
            return mp3, "audio/mpeg"

        wav = audio_mod.pcm16_to_wav(pcm, sample_rate=_TTS_SAMPLE_RATE)
        logger.info("tts: 합성 성공 WAV(ffmpeg 없음 → 폴백, %d bytes).", len(wav))
        return wav, "audio/wav"
    except Exception as exc:  # noqa: BLE001 - 인증/비활성/임의 예외 graceful
        logger.warning("tts: 합성 실패(무시, None) — %s", exc)
        return None


def _extract_pcm(resp: "Any") -> bytes | None:
    """genai 응답에서 첫 audio inline_data 바이트(raw PCM)를 추출(graceful None)."""
    try:
        for part in resp.candidates[0].content.parts:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline else None
            if data:
                return data
    except Exception:  # noqa: BLE001 - 응답 구조 예외 graceful
        return None
    return None
