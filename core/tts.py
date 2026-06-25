"""표현 TTS 합성 — Google Cloud Text-to-Speech(Chirp 3 HD, 한국어) graceful 어댑터.

통화후 분석이 배운 표현마다 호출해 한국어 MP3 를 만든다. import 실패·인증 실패·
API 비활성·임의 예외를 모두 흡수해 None 을 반환한다(speechsuper.py 와 동일 규율) —
TTS 가 안 돼도 분석 흐름(추출/번역/요약/저장)은 죽지 않는다.

인증: Vertex 와 동일한 서비스계정. settings.GOOGLE_APPLICATION_CREDENTIALS →
프로젝트 루트 gcp_key.json → ADC 순으로 자동 폴백.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any

from core.config import settings

logger = logging.getLogger(__name__)

# Chirp 3 HD 한국어 음성명. Cloud TTS 활성화 후 실제 음성ID 검증 필요(실패 시 None 이라 안전).
_VOICE_NAME = "ko-KR-Chirp3-HD-Charon"
_LANGUAGE_CODE = "ko-KR"

_client: "Any | None" = None
_client_ready = False


def _resolve_credentials_path() -> str | None:
    """서비스계정 키 경로 해석: 설정값 → 프로젝트 루트 gcp_key.json → None(ADC)."""
    import os

    cfg = settings.GOOGLE_APPLICATION_CREDENTIALS
    if cfg and pathlib.Path(cfg).is_file():
        return cfg
    env_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env_path and pathlib.Path(env_path).is_file():
        return env_path
    local = pathlib.Path(__file__).resolve().parents[1] / "gcp_key.json"
    if local.is_file():
        return str(local)
    return None


def _get_client() -> "Any | None":
    """Cloud TextToSpeech 클라이언트를 lazy 생성(없으면 None, 1회 경고)."""
    global _client, _client_ready
    if _client_ready:
        return _client

    _client_ready = True
    try:
        from google.cloud import texttospeech
        from google.oauth2 import service_account

        key_path = _resolve_credentials_path()
        if key_path:
            creds = service_account.Credentials.from_service_account_file(key_path)
            _client = texttospeech.TextToSpeechClient(credentials=creds)
        else:
            _client = texttospeech.TextToSpeechClient()  # ADC
        logger.info("tts: Cloud TTS 클라이언트 초기화 완료.")
    except Exception as exc:  # noqa: BLE001 - 미설치/인증/임의 예외 graceful
        logger.warning("tts: Cloud TTS 비활성(분석은 정상 진행) — %s", exc)
        _client = None
    return _client


def synthesize_korean(text: str) -> bytes | None:
    """한국어 텍스트를 Chirp 3 HD 로 합성해 MP3 bytes 반환(graceful None).

    입력이 비었거나 TTS 비활성/실패면 None — 호출부는 None 이면 TTS 를 건너뛴다.
    """
    if not text or not text.strip():
        return None

    client = _get_client()
    if client is None:
        return None

    try:
        from google.cloud import texttospeech

        response = client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text.strip()),
            voice=texttospeech.VoiceSelectionParams(
                language_code=_LANGUAGE_CODE, name=_VOICE_NAME
            ),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3
            ),
        )
        audio = response.audio_content
        if not audio:
            logger.warning("tts: 합성 결과 비어있음 → None.")
            return None
        logger.info("tts: 합성 성공(%d bytes).", len(audio))
        return audio
    except Exception as exc:  # noqa: BLE001 - 인증/비활성/음성명 오류 graceful
        logger.warning("tts: 합성 실패(무시, None) — %s", exc)
        return None
