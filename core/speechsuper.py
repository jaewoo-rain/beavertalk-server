"""발음 채점 어댑터 (SpeechSuper).

기준 문장(ref_text)과 녹음(audio_url)을 SpeechSuper 발음평가 API 로 채점한 뒤,
우리 도메인 형태(글자별 상/중/하 + 평가 점수)로 매핑해서 반환한다.

호출 명세(공식 샘플 기준):
- URL    : https://api.speechsuper.com/{coreType}   (POST, multipart/form-data)
- coreType: 한국어 문장 평가 "sent.eval.kr" (설정 SPEECH_SUPER_CORETYPE 로 교체 가능).
- 인증   : appKey + secretKey + timestamp 의 SHA1 서명
- 전송   : field text=json.dumps(params), file audio=오디오 바이트, header Request-Index: 0
- 응답   : result.{overall, pronunciation, fluency, rhythm, integrity} (0~100)
           + words[]: 한국어는 항목 1개=글자 1개, {word(글자), scores.overall(점수)}

실측 확인(sent.eval.kr, "안녕하세요"):
  result.overall/pronunciation/fluency/rhythm + words=[{word:"안",scores.overall:100}, …].
  → words[] 를 글자 단위로 펼치면 글자별 점수가 정확히 나온다(_map_char_scores).

폴백 정책(중요):
- 키 없음 / 네트워크 예외 / 타임아웃 / "result" 없음 / 파싱 실패 → 전부 잡아서
  결정적 스텁(_stub_assess)으로 폴백한다. 즉 이 함수는 절대 예외를 던지지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Optional

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

# SpeechSuper SDK 식별값(공식 샘플 고정값)
_SDK_VERSION = 16777472
_SDK_SOURCE = 9
_SDK_PROTOCOL = 2
_USER_ID = "guest"

# httpx 타임아웃: connect 5s / read 30s
_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)

# 오디오 확장자 → SpeechSuper audioType
_AUDIO_TYPES = {
    ".wav": "wav",
    ".mp3": "mp3",
    ".m4a": "m4a",
    ".ogg": "ogg",
    ".amr": "amr",
    ".aac": "aac",
    ".flac": "flac",
}


def _grade(score: int) -> str:
    """점수(0~100) → 상/중/하."""
    if score >= 85:
        return "상"
    if score >= 70:
        return "중"
    return "하"


def assess_pronunciation(ref_text: Optional[str], audio_url: Optional[str] = None) -> dict:
    """기준 문장(ref_text)에 대한 발음 채점 결과.

    Args:
        ref_text: 기준 문장(korean_sentence). None/빈문자면 빈 결과.
        audio_url: 녹음 위치. http(s) URL 또는 로컬 파일 경로. None 이면 즉시 스텁 폴백.

    Returns:
        {
          "evaluation": {total_score, pronunciation, fluency, rhythm},  # 0~100 int
          "char_scores": [{char, score, grade}, ...]                    # 공백 제외, ref_text 글자순
        }

    Note:
        SpeechSuper 호출에 실패하거나(키 없음/네트워크/타임아웃/스키마 불일치) 응답에
        "result" 가 없으면 결정적 스텁(_stub_assess)으로 폴백한다. 예외를 던지지 않는다.
    """
    # 오디오가 없으면 외부 호출 불가 → 스텁
    if not audio_url:
        return _stub_assess(ref_text)

    app_key = settings.SPEECH_SUPER_APP_KEY
    secret_key = settings.SPEECH_SUPER_SECRET_KEY
    if not app_key or not secret_key:
        logger.warning("SpeechSuper 키 미설정 → 스텁 폴백 사용")
        return _stub_assess(ref_text)

    try:
        audio_bytes, audio_type = _load_audio(audio_url)
        result = _call_speechsuper(
            ref_text=ref_text or "",
            audio_bytes=audio_bytes,
            audio_type=audio_type,
            app_key=app_key,
            secret_key=secret_key,
            core_type=settings.SPEECH_SUPER_CORETYPE,
        )
        return _map_result(ref_text, result)
    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 앱이 깨지면 안 됨
        logger.warning("SpeechSuper 호출/매핑 실패 → 스텁 폴백: %s", exc)
        return _stub_assess(ref_text)


# ──────────────────────────────────────────────────────────────────────────
# 오디오 취득
# ──────────────────────────────────────────────────────────────────────────
def _audio_type_from_path(path: str) -> str:
    """경로/URL 확장자에서 audioType 추론(기본 'wav')."""
    # URL 쿼리스트링 제거 후 확장자 검사
    clean = path.split("?", 1)[0].split("#", 1)[0]
    ext = os.path.splitext(clean)[1].lower()
    return _AUDIO_TYPES.get(ext, "wav")


def _load_audio(audio_url: str) -> tuple[bytes, str]:
    """audio_url 에서 오디오 바이트와 audioType 을 얻는다.

    http(s) 면 httpx GET, 그 외는 로컬 파일 경로로 간주해 read.
    """
    audio_type = _audio_type_from_path(audio_url)
    if audio_url.startswith(("http://", "https://")):
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(audio_url)
            resp.raise_for_status()
            return resp.content, audio_type
    with open(audio_url, "rb") as f:
        return f.read(), audio_type


# ──────────────────────────────────────────────────────────────────────────
# SpeechSuper 호출
# ──────────────────────────────────────────────────────────────────────────
def _sha1(text: str) -> str:
    """SHA1 hexdigest."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _build_params(
    *,
    ref_text: str,
    audio_type: str,
    app_key: str,
    secret_key: str,
    core_type: str,
    timestamp: str,
) -> dict[str, Any]:
    """SpeechSuper text 파라미터(connect+start) 구성.

    서명:
      connectSig = sha1(appKey + timestamp + secretKey)
      startSig   = sha1(appKey + timestamp + userId + secretKey)
    """
    connect_sig = _sha1(app_key + timestamp + secret_key)
    start_sig = _sha1(app_key + timestamp + _USER_ID + secret_key)
    return {
        "connect": {
            "cmd": "connect",
            "param": {
                "sdk": {
                    "version": _SDK_VERSION,
                    "source": _SDK_SOURCE,
                    "protocol": _SDK_PROTOCOL,
                },
                "app": {
                    "applicationId": app_key,
                    "sig": connect_sig,
                    "timestamp": timestamp,
                },
            },
        },
        "start": {
            "cmd": "start",
            "param": {
                "app": {
                    "userId": _USER_ID,
                    "applicationId": app_key,
                    "timestamp": timestamp,
                    "sig": start_sig,
                },
                "audio": {
                    "audioType": audio_type,
                    "channel": 1,
                    "sampleBytes": 2,
                    "sampleRate": 16000,
                },
                "request": {
                    "coreType": core_type,
                    "refText": ref_text,
                    "tokenId": f"tok-{timestamp}-{os.getpid()}",
                },
            },
        },
    }


def _call_speechsuper(
    *,
    ref_text: str,
    audio_bytes: bytes,
    audio_type: str,
    app_key: str,
    secret_key: str,
    core_type: str,
) -> dict[str, Any]:
    """SpeechSuper API 호출 → result dict 반환.

    HTTP 200 이어도 응답에 "result" 가 없으면 에러로 간주하고 예외를 던진다
    (상위에서 잡아 스텁 폴백).
    """
    timestamp = str(int(time.time()))
    params = _build_params(
        ref_text=ref_text,
        audio_type=audio_type,
        app_key=app_key,
        secret_key=secret_key,
        core_type=core_type,
        timestamp=timestamp,
    )
    url = f"https://api.speechsuper.com/{core_type}"
    data = {"text": json.dumps(params)}
    files = {"audio": ("audio." + audio_type, audio_bytes)}
    headers = {"Request-Index": "0"}

    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(url, data=data, files=files, headers=headers)
        resp.raise_for_status()
        body = resp.json()

    result = body.get("result")
    if not isinstance(result, dict):
        # 인증오류/한도초과 등도 보통 여기로 들어온다 → 에러로 간주
        raise ValueError(f"SpeechSuper 응답에 result 없음: {body!r}")
    return result


# ──────────────────────────────────────────────────────────────────────────
# 응답 매핑
# ──────────────────────────────────────────────────────────────────────────
def _to_int(value: Any, default: int = 0) -> int:
    """0~100 점수를 int 로 반올림. 변환 실패 시 default."""
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _map_result(ref_text: Optional[str], result: dict[str, Any]) -> dict:
    """SpeechSuper result → 도메인 반환 형태로 매핑."""
    overall = _to_int(result.get("overall"))
    pronunciation = _to_int(result.get("pronunciation"), overall)
    fluency = _to_int(result.get("fluency"), overall)
    # rhythm 없으면 integrity, 그것도 없으면 overall 로 대체
    rhythm = result.get("rhythm")
    if rhythm is None:
        rhythm = result.get("integrity")
    rhythm = _to_int(rhythm, overall)

    evaluation = {
        "total_score": overall,
        "pronunciation": pronunciation,
        "fluency": fluency,
        "rhythm": rhythm,
    }
    char_scores = _map_char_scores(ref_text, result, overall)
    return {"evaluation": evaluation, "char_scores": char_scores}


def _extract_word_scores(result: dict[str, Any]) -> list[tuple[str, int]]:
    """words[] 에서 (word, score) 목록을 추출(가능한 한 방어적으로)."""
    pairs: list[tuple[str, int]] = []
    words = result.get("words")
    if not isinstance(words, list):
        return pairs
    for w in words:
        if not isinstance(w, dict):
            continue
        word = w.get("word") or w.get("text") or ""
        # 단어 점수 후보들 중 먼저 잡히는 것을 사용
        score_val = (
            w.get("scores", {}).get("overall")
            if isinstance(w.get("scores"), dict)
            else None
        )
        if score_val is None:
            score_val = w.get("score")
        if score_val is None and isinstance(w.get("scores"), dict):
            score_val = w["scores"].get("pronunciation")
        pairs.append((str(word), _to_int(score_val) if score_val is not None else -1))
    return pairs


def _map_char_scores(
    ref_text: Optional[str], result: dict[str, Any], overall: int
) -> list[dict]:
    """글자별 char_scores 생성.

    실측 확인(coreType=sent.eval.kr): 한국어 응답의 `words[]`는 **항목 하나가 곧
    한 글자(음절)** 이며 `word`(글자)와 `scores.overall`(점수)를 갖는다.
    예: "안녕하세요" → words=[{word:"안",scores.overall:100}, {word:"녕",100},
    {word:"하",80}, {word:"세",87}, {word:"요",97}]. 따라서 words[]를 글자 단위로
    그대로 펼치면 추정 없이 정확히 매핑된다.

    words[]를 못 얻는 경우(영어 다른 coreType/스키마 변형/응답 누락)에만 ref_text 의
    공백 제외 글자에 overall 기준 결정적 점수를 채우는 폴백을 쓴다.
    """
    word_pairs = [(w, s) for (w, s) in _extract_word_scores(result) if s >= 0]

    out: list[dict] = []
    for word_text, score in word_pairs:
        for ch in word_text:
            if ch.isspace():
                continue
            s = max(0, min(100, score))
            out.append({"char": ch, "score": s, "grade": _grade(s)})
    if out:
        return out

    # 폴백: words[] 없음 → ref_text 글자에 overall 기준 결정적 점수
    chars = [c for c in (ref_text or "") if not c.isspace()]
    for ch in chars:
        score = max(0, min(100, overall + ((ord(ch) % 5) - 2)))
        out.append({"char": ch, "score": score, "grade": _grade(score)})
    return out


# ──────────────────────────────────────────────────────────────────────────
# 폴백 스텁 (기존 결정적 로직 보존)
# ──────────────────────────────────────────────────────────────────────────
def _stub_assess(ref_text: Optional[str]) -> dict:
    """[STUB 폴백] 외부 호출 없이 결정적 채점 결과 생성.

    SpeechSuper 호출이 불가/실패할 때 사용. 반환 계약은 assess_pronunciation 과 동일.
    """
    chars = [c for c in (ref_text or "") if not c.isspace()]
    char_scores = [
        {"char": c, "score": (s := 60 + (ord(c) % 41)), "grade": _grade(s)}
        for c in chars
    ]
    avg = round(sum(c["score"] for c in char_scores) / len(char_scores)) if char_scores else 0
    return {
        "evaluation": {
            "total_score": avg,
            "pronunciation": avg,
            "fluency": max(0, avg - 5),
            "rhythm": max(0, avg - 3),
        },
        "char_scores": char_scores,
    }
