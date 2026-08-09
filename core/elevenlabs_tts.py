"""ElevenLabs TTS — 캐스케이드 통화용 스트리밍 어댑터(도메인·DB 무지).

⛔ 구글이 아니다. `google-cloud-texttospeech` SDK 를 못 쓴다 — REST 스트리밍을 직접 부른다.
   그래서 파일을 따로 둔다(core/tts.py 는 Cloud TTS 전용으로 그대로 둔다).

문서에서 확인한 것(2026-08-08, 공식 문서 원문):
  - 엔드포인트: `POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream`
  - 인증 헤더 : "All API requests should include your API key in an `xi-api-key` HTTP header."
  - 출력 포맷 : output_format 허용값에 **pcm_24000** 이 있다(mp3·opus·ulaw 등과 나란히).
    ⭐ 우리 파이프라인이 PCM16/24k 라 **디코딩 없이 그대로 꽂힌다** — MP3 만 됐으면 디코더가
      붙고 지연·복잡도가 늘었을 자리다.
  - 모델 ID  : `eleven_flash_v2_5`("Ultra-low latency (~75ms†)", 32개 언어) / `eleven_v3`(70+ 언어)
  - 감정 태그: v3 는 **텍스트에 인라인**으로 넣는다(`[laughs]` `[whispers]` 같은 오디오 태그).
    별도 필드가 아니다.

⚠ **확인 못 한 것**(추측하지 않고 그대로 적는다):
  - pcm_24000 의 비트 깊이·엔디안이 문서에 명시돼 있지 않다. 통상 S16LE 이고 우리 규약도
    그것이라 그대로 흘리지만, **첫 통화에서 앞 8바이트 로그로 확인해야 한다**(core/tts.py 와
    같은 방식). 다르면 소리가 잡음으로 들린다 — 그때 바로 드러난다.
  - v3 의 스트리밍 적합성: 문서는 v3 를 "Character Discussions / Audiobook / Emotional
    Dialogue" 에 좋다고 하고 **실시간 에이전트에는 Flash v2.5 를 권한다.** 스트리밍 엔드포인트
    자체는 v3 도 받는다(모델 ID 로 지정). 즉 "안 된다"가 아니라 "느릴 수 있다"로 읽힌다 —
    느리면 배치 모드(gemini-batch 와 같은 경로)로 돌리면 된다.
  - 실제 지연·품질은 **키가 없어 못 쟀다.** 배포 후 첫 통화가 답한다.

R5: 키가 없거나 호출이 실패하면 **예외를 밖으로 내지 않고** 빈 스트림으로 끝낸다. 호출부가
그걸 보고 다른 엔진으로 폴백하거나 그 문장을 건너뛴다 — 앱이 죽으면 안 된다.
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator

from core.config import settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.elevenlabs.io/v1/text-to-speech"
# ⛔ 원가 벤더 문자열이기도 하다 — 단가표 키와 정확히 같아야 원가가 '미상'으로 안 뜬다.
#   ⚠ **모델별로 단가가 다르다.** flash 와 v3 를 뭉개면 원가를 못 가른다(Gemini flash/pro 에서
#     배운 그대로다).
FLASH_MODEL = "eleven_flash_v2_5"
V3_MODEL = "eleven_v3"
# ⭐ 사장님 목적은 **속도가 아니라 목소리**다("AI 티가 나서 일레븐랩스를 쓰려는 거야").
#   1차 자료(https://elevenlabs.io/docs/models, 2026-08-09):
#     flash          "Our fast, affordable speech synthesis model" · ~75ms · 32개 언어
#     multilingual_v2 "**Lifelike, consistent quality** speech synthesis model" · 29개 언어
#                     "Most stable on long-form generations" · **실시간 최적화 아님**
#     v3             "Our most emotionally rich, expressive speech synthesis model" · 70+ 언어
#   ⛔ 빠른 모델은 표현력을 깎아서 빠르다 — flash 만으로는 "AI 티"가 그대로 남을 수 있다.
#   그래서 **중간 등급**을 후보로 둔다. 셋 다 한국어를 지원한다(문서 언어 목록).
MULTILINGUAL_MODEL = "eleven_multilingual_v2"
# 우리 파이프라인 규약(PCM16 / 24kHz mono)과 정확히 맞는 값.
OUTPUT_FORMAT = "pcm_24000"
_FIRST_BYTES_LOGGED: set[str] = set()


def is_configured() -> bool:
    """키가 있나 — 없으면 이 엔진은 **비활성**이다(고르면 명확한 에러로 알린다)."""
    return bool((settings.CASCADE_TTS_ELEVEN_API_KEY or "").strip())


async def synthesize_stream(
    text: str,
    *,
    model_id: str,
    voice_id: str | None = None,
    speaking_rate: float | None = None,
    report: dict | None = None,
) -> AsyncIterator[bytes]:
    """텍스트 → PCM16/24k 조각(async generator). 실패·키부재면 **아무것도 안 낸다**(R5).

    ⚠ 감정 태그(v3)는 **텍스트 안에** 들어간다(`[laughs]` 등) — 별도 필드가 아니라서 여기서는
      텍스트를 그대로 넘긴다. 태그를 넣는 건 프롬프트(LLM)의 몫이다.
    """
    body_text = (text or "").strip()
    if not body_text:
        return
    key = (settings.CASCADE_TTS_ELEVEN_API_KEY or "").strip()
    if not key:
        logger.warning("elevenlabs: API 키 없음 → 이 엔진 비활성(합성 건너뜀)")
        return
    voice = (voice_id or settings.CASCADE_TTS_ELEVEN_VOICE_ID or "").strip()
    if not voice:
        logger.warning("elevenlabs: voice_id 미설정 → 합성 건너뜀")
        return

    try:
        import httpx
    except Exception as exc:  # noqa: BLE001 - 미설치 graceful
        logger.warning("elevenlabs: httpx 미설치(무시) — %s", exc)
        return

    payload: dict[str, Any] = {"text": body_text, "model_id": model_id}
    # ⚠ 속도 손잡이는 구글과 이름·범위가 다를 수 있다. 문서로 확인 못 해서 **기본값에서는
    #   아무것도 안 보낸다** — 모르는 필드를 넣으면 요청이 통째로 거절될 수 있다.
    #   (구글 쪽 speaking_rate 는 core/tts.py 가 그대로 쓴다.)
    if speaking_rate is not None and speaking_rate != 1.0:
        payload["voice_settings"] = {"speed": float(speaking_rate)}

    url = f"{API_BASE}/{voice}/stream?output_format={OUTPUT_FORMAT}"
    headers = {"xi-api-key": key, "Content-Type": "application/json"}
    asked_at = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread())[:200]
                    # ⛔ 조용히 죽지 않는다 — 어느 모델이 왜 거절됐는지 이 줄로 갈린다.
                    #   ⚠ 키는 절대 로그에 넣지 않는다.
                    logger.warning(
                        "elevenlabs 실패: model=%s status=%s 사유=%r",
                        model_id, resp.status_code, detail,
                    )
                    if report is not None and resp.status_code == 429:
                        report["quota"] = True
                    return
                first = True
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    if first:
                        first = False
                        if report is not None:
                            report["engine"] = model_id
                            # 벤더가 첫 오디오를 주기까지(첫소리 분해의 '벤더' 항목).
                            report["ttfb_ms"] = int((time.monotonic() - asked_at) * 1000)
                        _log_first_bytes(chunk, model_id)
                    yield chunk
    except Exception as exc:  # noqa: BLE001 - 네트워크·파싱 등 전부 graceful(R5)
        logger.warning("elevenlabs 실패: model=%s 사유=%r", model_id, str(exc)[:200])


def _log_first_bytes(chunk: bytes, model_id: str) -> None:
    """첫 조각 앞 8바이트를 **모델당 한 번**. pcm_24000 의 비트 깊이·엔디안이 문서에 없어서,
    실제로 raw PCM 이 오는지(헤더가 안 붙는지) 이 한 줄로 확인한다."""
    if model_id in _FIRST_BYTES_LOGGED:
        return
    _FIRST_BYTES_LOGGED.add(model_id)
    logger.info(
        "elevenlabs: 첫 조각 model=%s 앞 8바이트=%s (RIFF/ID3 면 헤더 포함 = 규약 불일치)",
        model_id, chunk[:8].hex(" "),
    )
