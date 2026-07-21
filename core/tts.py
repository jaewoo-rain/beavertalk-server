"""표현 TTS 합성 — Google Cloud Text-to-Speech(Chirp3-HD, 다국어) graceful 어댑터.

통화후 분석이 배운 표현마다 호출해 대상 언어 오디오를 만든다.

⚠️ 신원 분리(멀티랭귀지):
    - Gemini Live(통화 음성·STT)·분석·구 Gemini-TTS 는 Vertex(tta-lingko-rookie, gcp_key.json).
      그 프로젝트는 **빌린 것**이라 Cloud TTS 를 못 켠다.
    - 그래서 TTS 는 **우리 소유 프로젝트 bt-dev-web-01** 의 서비스계정 키(tts_key.json)로
      Cloud Text-to-Speech 를 직접 호출한다. 두 신원이 공존한다.

장점 vs 구 Gemini-TTS:
    - 언어별 네이티브 음성(ko/ja/en/zh/fr/vi Chirp3-HD) — 구현은 ko-KR 하드코딩이라 일본어를
      한국어 발음으로 읽던 버그를 해소.
    - **MP3 를 직접** 받는다 → ffmpeg 불필요(구현은 raw PCM→ffmpeg 라 cpu=1 에서 타임아웃).
    - 클래식 TTS 라 지연이 낮다(생성형 모델 콜 대비).

import/인증/비활성/임의 예외를 모두 흡수해 None 을 반환한다(speechsuper.py 와 동일 규율) —
TTS 가 안 돼도 분석 흐름(추출/번역/요약/저장)은 죽지 않는다.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.config import settings

logger = logging.getLogger(__name__)

# 언어(ISO 코드) → (BCP-47 languageCode, Chirp3-HD 음성명). 미지원 언어는 ko 폴백.
# 6개 언어 모두 Chirp3-HD 30종 확인(2026-07). Aoede=여성 표준 음성으로 통일.
_VOICE_BY_LANG: dict[str, tuple[str, str]] = {
    "ko": ("ko-KR", "ko-KR-Chirp3-HD-Aoede"),
    "ja": ("ja-JP", "ja-JP-Chirp3-HD-Aoede"),
    "en": ("en-US", "en-US-Chirp3-HD-Aoede"),
    "zh": ("cmn-CN", "cmn-CN-Chirp3-HD-Aoede"),
    "fr": ("fr-FR", "fr-FR-Chirp3-HD-Aoede"),
    "vi": ("vi-VN", "vi-VN-Chirp3-HD-Aoede"),
}
# 라벨 → 코드(하위호환: 호출부가 "일본어" 같은 라벨을 넘겨도 해석).
_LABEL_TO_CODE: dict[str, str] = {
    "한국어": "ko", "일본어": "ja", "영어": "en",
    "중국어": "zh", "프랑스어": "fr", "베트남어": "vi",
}


def _resolve_voice(language: str) -> tuple[str, str]:
    """언어(코드 'ja' 또는 라벨 '일본어') → (languageCode, voice_name). 미상은 ko."""
    code = _LABEL_TO_CODE.get((language or "").strip(), (language or "ko").strip().lower())
    return _VOICE_BY_LANG.get(code, _VOICE_BY_LANG["ko"])


@lru_cache(maxsize=1)
def _client() -> "Any | None":
    """Cloud TTS 비동기 클라이언트(프로세스당 1개). 키 부재·미설치·인증실패면 None(graceful)."""
    try:
        key_path = Path(settings.TTS_SA_KEY_FILE)
        if not key_path.is_file():
            logger.warning("tts(gcp): SA 키 없음(%s) → TTS 비활성.", key_path)
            return None
        from google.cloud import texttospeech
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            str(key_path), scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        client = texttospeech.TextToSpeechAsyncClient(credentials=creds)
        logger.info("tts(gcp): TextToSpeech async client ready (project=%s)",
                    getattr(creds, "project_id", None))
        return client
    except Exception as exc:  # noqa: BLE001 - 미설치/인증 등 graceful
        logger.warning("tts(gcp): 클라이언트 초기화 실패 → 비활성: %s", exc)
        return None


async def synthesize(
    text: str, language: str = "ko", client: "Any | None" = None
) -> tuple[bytes, str] | None:
    """텍스트를 대상 언어 Chirp3-HD 음성으로 합성 → (mp3_bytes, "audio/mpeg") 또는 None.

    (멀티랭귀지) language 는 ISO 코드("ja") 또는 라벨("일본어"). Cloud TTS 는 MP3 를 직접
    주므로 ffmpeg 불필요. client 인자는 하위호환용(구 Gemini 클라이언트) — 무시한다.
    키 부재/합성 실패는 None(graceful) — 호출부는 None 이면 TTS 를 건너뛴다.
    """
    if not text or not text.strip():
        return None
    cli = _client()
    if cli is None:
        return None
    lang_code, voice_name = _resolve_voice(language)
    try:
        from google.cloud import texttospeech

        resp = await cli.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text.strip()),
            voice=texttospeech.VoiceSelectionParams(language_code=lang_code, name=voice_name),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3
            ),
        )
        if not resp.audio_content:
            logger.warning("tts(gcp): 합성 결과 비어있음 → None.")
            return None
        logger.info("tts(gcp): 합성 성공 MP3(%d bytes, voice=%s).",
                    len(resp.audio_content), voice_name)
        return resp.audio_content, "audio/mpeg"
    except Exception as exc:  # noqa: BLE001 - 인증/비활성/임의 예외 graceful
        logger.warning("tts(gcp): 합성 실패(무시, None) — %s", exc)
        return None
