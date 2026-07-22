"""국적 분류 어댑터 (외부 오디오 국적 추론 API).

user 발화 오디오를 외부 국적 분류 서버로 보내 상위 후보(country/iso/prob)를 받는다.
core.speechsuper 와 동일한 규율을 따른다:

- 도메인/DB 를 모르는 순수 어댑터(포트). 외부 세부를 도메인으로 새게 하지 않는다.
- **이 함수는 절대 예외를 던지지 않는다.** 어떤 실패(미설정/네트워크/타임아웃/5xx/
  발화부족/파싱오류)든 잡아서 None 을 반환한다 → 호출측(통화 파이프라인)이 국적 갱신만
  조용히 스킵하고 통화·분석은 그대로 진행(graceful degradation, R5).

호출 명세(실측 계약, 그대로):
- URL     : POST {NATIONALITY_API_URL}/predict?top_k={NATIONALITY_API_TOP_K}
- 전송    : multipart/form-data, field `file` = 오디오 바이트
- 성공    : {"predictions":[{"country","iso","prob"}, ...], "top1",
             "duration_sec", "latency_ms"}
- 발화부족: {"predictions": null, "top1": null, "reason": "no_speech_detected"}

정책:
- URL 미설정 → None(모듈당 1회만 warning).
- predictions 가 None/누락(no_speech 포함) → None.
- 정상 predictions 리스트 → **응답 body dict 를 그대로 반환**(가공 없음).
- 재시도: 네트워크 예외·타임아웃·5xx 만 1회(짧은 백오프). 4xx·no_speech 는 재시도 안 함.
- 로깅: 성공 시 top1·latency 만 info. 국적은 PII 이므로 원본 오디오·상세 prob 대량 로깅 금지.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

# 오디오 확장자(audio_type) → multipart MIME
_MIME_TYPES = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
}
_DEFAULT_MIME = "application/octet-stream"

# 재시도: 5xx·네트워크·타임아웃만 1회, 짧은 백오프
_MAX_ATTEMPTS = 2
_RETRY_BACKOFF_S = 0.5

# URL 미설정 warning 을 모듈당 1회만 남기기 위한 플래그
_warned_no_url = False


def predict_nationality(audio_bytes: bytes, audio_type: str = "wav") -> Optional[dict]:
    """오디오 바이트로 국적을 추론한다.

    Args:
        audio_bytes: user 발화 오디오 바이트(예: WAV PCM16/16k).
        audio_type: 확장자 힌트("wav"/"mp3"). MIME 결정에만 사용.

    Returns:
        성공 시 외부 API 응답 body dict 그대로
        ({"predictions": [...], "top1": ..., ...}).
        미설정 / 발화부족(no_speech) / 불통 / 파싱실패 등 모든 실패 → None.
        (이 함수는 예외를 던지지 않는다.)
    """
    global _warned_no_url

    base_url = settings.NATIONALITY_API_URL
    if not base_url:
        if not _warned_no_url:
            logger.warning("국적 API(NATIONALITY_API_URL) 미설정 → 국적 추론 비활성(None)")
            _warned_no_url = True
        return None

    if not audio_bytes:
        logger.warning("국적 API: 빈 오디오 → 스킵(None)")
        return None

    url = base_url.rstrip("/") + "/predict"
    params = {"top_k": settings.NATIONALITY_API_TOP_K}
    mime = _MIME_TYPES.get(audio_type.lower(), _DEFAULT_MIME)
    timeout = httpx.Timeout(
        connect=5.0,
        read=settings.NATIONALITY_API_TIMEOUT_S,
        write=settings.NATIONALITY_API_TIMEOUT_S,
        pool=5.0,
    )

    try:
        body = _call_with_retry(
            url=url, params=params, audio_bytes=audio_bytes, mime=mime,
            audio_type=audio_type, timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 통화가 깨지면 안 됨
        logger.warning("국적 API 호출 실패 → None: %s", exc)
        return None

    if body is None:
        return None

    predictions = body.get("predictions") if isinstance(body, dict) else None
    if not isinstance(predictions, list) or not predictions:
        # predictions=null/누락(no_speech 포함) 또는 빈 리스트 → 조용히 None
        reason = body.get("reason") if isinstance(body, dict) else None
        logger.info("국적 API: 예측 없음(reason=%s) → None", reason)
        return None

    logger.info(
        "국적 API 성공: top1=%s latency_ms=%s",
        body.get("top1"),
        body.get("latency_ms"),
    )
    return body


def _call_with_retry(
    *,
    url: str,
    params: dict,
    audio_bytes: bytes,
    mime: str,
    audio_type: str,
    timeout: httpx.Timeout,
) -> Optional[dict]:
    """국적 API POST 호출. 재시도 대상(네트워크·타임아웃·5xx)만 최대 1회 재시도.

    - 2xx: json() 반환.
    - 4xx: 재시도하지 않고 예외 전파(상위에서 잡아 None).
    - 5xx / 네트워크 / 타임아웃: 짧은 백오프 후 재시도, 마지막 시도 실패 시 예외 전파.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            files = {"file": (f"audio.{audio_type}", audio_bytes, mime)}
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, params=params, files=files)
            status = resp.status_code
            if 500 <= status < 600:
                # 서버 오류 → 재시도 대상
                raise httpx.HTTPStatusError(
                    f"국적 API 5xx: {status}", request=resp.request, response=resp
                )
            # 4xx 는 raise_for_status 로 즉시 예외(재시도 안 함)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if 500 <= status < 600 and attempt < _MAX_ATTEMPTS:
                last_exc = exc
                time.sleep(_RETRY_BACKOFF_S)
                continue
            raise  # 4xx 또는 재시도 소진 → 상위로
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # 네트워크·타임아웃 → 재시도 대상
            if attempt < _MAX_ATTEMPTS:
                last_exc = exc
                time.sleep(_RETRY_BACKOFF_S)
                continue
            raise
    # 이론상 도달 불가(마지막 시도는 return 하거나 raise). 방어적.
    if last_exc is not None:
        raise last_exc
    return None
