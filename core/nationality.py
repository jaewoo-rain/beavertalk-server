"""국적 분류 어댑터 (외부 오디오 국적 추론 API).

user 발화 오디오를 외부 국적 분류 서버로 보내 상위 후보(country/iso/prob)를 받는다.
core.speechsuper 와 동일한 규율을 따른다:

- 도메인/DB 를 모르는 순수 어댑터(포트). 외부 세부를 도메인으로 새게 하지 않는다.
- **이 함수는 절대 예외를 던지지 않는다.** 어떤 실패(미설정/네트워크/타임아웃/5xx/
  발화부족/파싱오류)든 잡아서 None 을 반환한다 → 호출측(통화 파이프라인)이 국적 갱신만
  조용히 스킵하고 통화·분석은 그대로 진행(graceful degradation, R5).

호출 명세(2026-09-14 GPU 서버 계약 — nationality-api-guide.md):
- URL     : POST {NATIONALITY_API_URL}/predict
- 인증    : X-API-Key: {NATIONALITY_API_KEY} (서버 앞단 프록시가 검사. 비면 헤더 생략)
- 전송    : multipart/form-data, field `audio` = 오디오 바이트
- 성공    : {"top5":[{"label","prob"}, ...], "sec", "encode_ms", "total_ms"}
- 실패    : 400 {"error","code"} — code ∈ no_speech·too_short·decode_failed
            422 audio 필드 누락 / 401 키 불일치 / 500 server_decoder

반환(내부 형태 — nationality_service 가 이 모양을 전제한다):
    {"predictions":[{"country","iso","prob"}], "top1", "duration_sec", "latency_ms"}
  서버는 iso 를 주지 않으므로 _LABEL_ISO 로 붙인다. country 는 서버 label 그대로다.

정책:
- URL 미설정 → None(모듈당 1회만 warning).
- 400(no_speech 등) · top5 가 비었거나 형식 이상 → None.
- 재시도: 네트워크 예외·타임아웃·5xx 만 최대 2회(1초 → 2초, 서버 담당 권장값). 4xx 는 재시도 안 함.
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

# 재시도: 5xx·네트워크·타임아웃만. 시도 사이 대기(초) — 길이+1 이 총 시도 횟수다.
_RETRY_BACKOFFS_S = (1.0, 2.0)

# 국적 서버 라벨(모국어 기준 41개) → ISO 3166-1 alpha-2. 서버는 iso 를 주지 않는다.
_LABEL_ISO = {
    "Bangladesh": "BD", "Brazil": "BR", "Bulgaria": "BG", "Cambodia": "KH",
    "Chile": "CL", "China": "CN", "Colombia": "CO", "Ethiopia": "ET",
    "France": "FR", "Germany": "DE", "Hong Kong": "HK", "India": "IN",
    "Indonesia": "ID", "Iran": "IR", "Italy": "IT", "Japan": "JP",
    "Kazakhstan": "KZ", "Korea": "KR", "Kyrgyzstan": "KG", "Malaysia": "MY",
    "Mongolia": "MN", "Myanmar": "MM", "Nepal": "NP", "Pakistan": "PK",
    "Peru": "PE", "Philippines": "PH", "Romania": "RO", "Russia": "RU",
    "Rwanda": "RW", "Singapore": "SG", "Spain": "ES", "Tajikistan": "TJ",
    "Thailand": "TH", "Turkey": "TR", "Turkmenistan": "TM", "Ukraine": "UA",
    "United Kingdom": "GB", "United States": "US", "Uzbekistan": "UZ",
    "Vietnam": "VN", "Zambia": "ZM",
}


def iso_for_country(country_name: Optional[str]) -> Optional[str]:
    """국적 서버 라벨(영문명) → ISO 2자리. 표에 없는 이름이면 None.

    `speak_country` 에는 영문명만 남는데, 그 이름이 다른 테이블의 표기와 늘 같지는
    않다 — 실측(2026-09-21)으로 모델은 `Russia`, 취약발음 통계는 `Russian Federation`
    이었다. 이름끼리 맞추면 러시아 사용자만 조용히 목록이 비었다. 이름을 여기서 한 번
    ISO 로 접어서, 표기가 갈려도 같은 나라로 만나게 한다.
    """
    if not country_name:
        return None
    return _LABEL_ISO.get(country_name)


# URL 미설정 warning 을 모듈당 1회만 남기기 위한 플래그
_warned_no_url = False


def predict_nationality(audio_bytes: bytes, audio_type: str = "wav") -> Optional[dict]:
    """오디오 바이트로 국적을 추론한다.

    Args:
        audio_bytes: user 발화 오디오 바이트(예: WAV PCM16/16k).
        audio_type: 확장자 힌트("wav"/"mp3"). MIME 결정에만 사용.

    Returns:
        성공 시 내부 형태로 정규화한 dict
        ({"predictions": [{"country","iso","prob"}], "top1": ..., ...}).
        미설정 / 발화부족(400 no_speech 등) / 불통 / 파싱실패 등 모든 실패 → None.
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
    api_key = (settings.NATIONALITY_API_KEY or "").strip()
    headers = {"X-API-Key": api_key} if api_key else None
    mime = _MIME_TYPES.get(audio_type.lower(), _DEFAULT_MIME)
    timeout = httpx.Timeout(
        connect=5.0,
        read=settings.NATIONALITY_API_TIMEOUT_S,
        write=settings.NATIONALITY_API_TIMEOUT_S,
        pool=5.0,
    )

    try:
        body = _call_with_retry(
            url=url, headers=headers, audio_bytes=audio_bytes, mime=mime,
            audio_type=audio_type, timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 통화가 깨지면 안 됨
        logger.warning("국적 API 호출 실패 → None: %s", exc)
        return None

    if body is None:
        return None

    normalized = _normalize(body)
    if normalized is None:
        logger.info("국적 API: 예측 없음(top5 비었거나 형식 이상) → None")
        return None

    logger.info(
        "국적 API 성공: top1=%s latency_ms=%s",
        normalized["top1"],
        normalized["latency_ms"],
    )
    return normalized


def _normalize(body: object) -> Optional[dict]:
    """서버 응답(top5·label·sec·total_ms) → 내부 형태. 쓸 만한 후보가 없으면 None."""
    if not isinstance(body, dict):
        return None
    preds = []
    for item in body.get("top5") or []:
        if not isinstance(item, dict):
            continue
        label, prob = item.get("label"), item.get("prob")
        if not isinstance(label, str) or not label:
            continue
        if isinstance(prob, bool) or not isinstance(prob, (int, float)):
            continue
        iso = _LABEL_ISO.get(label)
        if iso is None:
            logger.warning("국적 API: 모르는 라벨 %r — iso 없이 적재", label)
        preds.append({"country": label, "iso": iso, "prob": float(prob)})
    if not preds:
        return None
    return {
        "predictions": preds,
        "top1": preds[0]["country"],
        "duration_sec": body.get("sec"),
        "latency_ms": body.get("total_ms"),
    }


def _call_with_retry(
    *,
    url: str,
    headers: Optional[dict],
    audio_bytes: bytes,
    mime: str,
    audio_type: str,
    timeout: httpx.Timeout,
) -> Optional[dict]:
    """국적 API POST 호출. 재시도 대상(네트워크·타임아웃·5xx)만 _RETRY_BACKOFFS_S 만큼 재시도.

    - 2xx: json() 반환.
    - 400: code 만 로그로 남기고 None(no_speech 등은 정상적인 "결과 없음"이다).
    - 그 외 4xx: 재시도하지 않고 예외 전파(상위에서 잡아 None).
    - 5xx / 네트워크 / 타임아웃: 백오프 후 재시도, 마지막 시도 실패 시 예외 전파.
    """
    attempts = len(_RETRY_BACKOFFS_S) + 1
    for attempt in range(attempts):
        try:
            files = {"audio": (f"audio.{audio_type}", audio_bytes, mime)}
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, files=files, headers=headers)
            status = resp.status_code
            if 500 <= status < 600:
                # 서버 오류 → 재시도 대상
                raise httpx.HTTPStatusError(
                    f"국적 API 5xx: {status}", request=resp.request, response=resp
                )
            if status == 400:
                logger.info("국적 API: 400 code=%s → None", _error_code(resp))
                return None
            if status == 401:
                logger.warning("국적 API: 401 — NATIONALITY_API_KEY 불일치 또는 미설정")
            # 그 외 4xx 는 raise_for_status 로 즉시 예외(재시도 안 함)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if 500 <= status < 600 and attempt < attempts - 1:
                time.sleep(_RETRY_BACKOFFS_S[attempt])
                continue
            raise  # 4xx 또는 재시도 소진 → 상위로
        except (httpx.TimeoutException, httpx.TransportError):
            # 네트워크·타임아웃 → 재시도 대상
            if attempt < attempts - 1:
                time.sleep(_RETRY_BACKOFFS_S[attempt])
                continue
            raise
    return None  # 이론상 도달 불가(마지막 시도는 return 하거나 raise). 방어적.


def _error_code(resp: object) -> Optional[str]:
    """실패 응답 body 의 code. JSON 이 아니거나 code 가 없으면 None."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - 로그용 보조 정보일 뿐이다
        return None
    return body.get("code") if isinstance(body, dict) else None
