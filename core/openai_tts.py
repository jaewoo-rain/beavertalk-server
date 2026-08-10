"""OpenAI TTS 어댑터 — `/v1/audio/speech` **HTTP 청크 스트리밍**(WebSocket 아니다).

배관을 새로 만들 필요가 없다. 1차 자료(https://developers.openai.com/api/docs/guides/
text-to-speech, 2026-08-10 확인)로 두 가지가 우리 규약과 정확히 맞는다:
  · `pcm` = "raw samples in **24kHz (16-bit signed, low-endian)**, without the header"
    → `CASCADE_TTS_SAMPLE_RATE`(24000) 와 같다. **변환이 필요 없다.**
  · "The Speech API provides support for realtime audio streaming using **chunk transfer
    encoding**" → 페이서가 조각을 받는 즉시 흘릴 수 있다(TTS 요건 H1).

⭐ `instructions` 가 우리 **스타일 프롬프트와 같은 자리**다 — 감정 태그 6종이 그대로 붙는다.

⛔ `speed` 파라미터는 **가이드에 없다.** 같은 문서가 gpt-4o-mini-tts 의 조절 가능한 항목으로
  "Speed of speech" 를 들지만 그건 **`instructions` 로 말로 부탁하는 것**이지 파라미터가 아니다.
  ⇒ 우리 **언어별 배속 레버에 연결하지 않았다.** 말로 부탁하는 길은 이미 막아 뒀다(스타일
    프롬프트 속도 어휘 금지 회귀) — 오늘 "또박또박" 한 낱말이 한국어 속도의 절반을 먹었다.
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

SPEECH_URL = "https://api.openai.com/v1/audio/speech"
DEFAULT_MODEL = "gpt-4o-mini-tts"
_VENDOR_PREFIX = "openai-"
_FIRST_BYTES_LOGGED: set[str] = set()


def is_configured() -> bool:
    """키가 있나. ⛔ 키 값은 로그·예외 어디에도 싣지 않는다."""
    return bool((settings.GPT_API_KEY or "").strip())


def vendor_name() -> str:
    """원가 벤더 = **모델 ID**(단가가 모델마다 다르다)."""
    return _VENDOR_PREFIX + (settings.CASCADE_TTS_OPENAI_MODEL or DEFAULT_MODEL).strip()


async def synthesize_stream(
    text: str,
    *,
    voice: str | None = None,
    instructions: str | None = None,
    report: dict | None = None,
) -> AsyncIterator[bytes]:
    """문장 하나를 합성해 **PCM16/24k 조각**을 흘린다. 실패하면 조용히 끝난다(R5).

    ⛔ 폴백하지 않는다 — 엔진을 골라 듣는 중인데 다른 소리가 나면 A/B 가 거짓말이 된다
      (Gemini→Chirp 폴백에서 배운 그대로. 그건 로그로 드러내고 벤더를 정정한다).
    """
    key = (settings.GPT_API_KEY or "").strip()
    if not key or not text.strip():
        return
    model = (settings.CASCADE_TTS_OPENAI_MODEL or DEFAULT_MODEL).strip()
    payload: dict[str, Any] = {
        "model": model,
        "voice": (voice or settings.CASCADE_TTS_OPENAI_VOICE or "nova").strip(),
        "input": text.strip(),
        "response_format": "pcm",     # 24kHz · 16-bit · mono · 헤더 없음(1차 자료)
    }
    if instructions:
        payload["instructions"] = instructions
    asked_at = time.monotonic()
    first = True
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST", SPEECH_URL, json=payload,
                headers={"Authorization": f"Bearer {key}"},   # ⛔ 키는 헤더로만
            ) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread())[:200]
                    # ⛔ 조용히 죽지 않는다 — 어느 모델이 왜 거절됐는지 이 줄로 갈린다.
                    logger.warning("openai-tts 실패: model=%s status=%s 사유=%r",
                                   model, resp.status_code, detail)
                    if report is not None and resp.status_code == 429:
                        report["quota"] = True
                    return
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    if first:
                        first = False
                        if report is not None:
                            report["engine"] = vendor_name()
                            report["ttfb_ms"] = int((time.monotonic() - asked_at) * 1000)
                        _log_first_bytes(chunk, model)
                    yield chunk
    except Exception as exc:  # noqa: BLE001 - 네트워크·파싱 전부 graceful(R5)
        logger.warning("openai-tts 실패: model=%s 사유=%r", model, str(exc)[:200])


def _log_first_bytes(chunk: bytes, model: str) -> None:
    """첫 조각 앞 8바이트를 **모델당 한 번**.

    ⚠ `pcm` 은 헤더가 없어야 한다. WAV(`RIFF`)가 섞여 오면 재생 큐에서 딸깍 소리가 나고,
      그건 원인 찾기가 오래 걸리는 증상이다 — 그래서 눈으로 확인할 수 있게 남긴다.
    """
    if model in _FIRST_BYTES_LOGGED:
        return
    _FIRST_BYTES_LOGGED.add(model)
    logger.info("openai-tts 첫 조각: model=%s %d바이트 앞8=%s", model, len(chunk),
                chunk[:8].hex())
