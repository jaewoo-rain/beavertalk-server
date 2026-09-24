"""표현 TTS 합성 — Google Cloud Text-to-Speech(Chirp3-HD, 다국어) graceful 어댑터.

통화후 분석이 배운 표현마다 호출해 대상 언어 오디오를 만든다.

⚠️ 신원 분리(멀티랭귀지):
    - Gemini Live(통화 음성·STT)·분석·구 Gemini-TTS 는 Vertex(tta-lingko-rookie, gcp_key.json).
      그 프로젝트는 **빌린 것**이라 Cloud TTS 를 못 켠다.
    - 그래서 TTS 는 **우리 소유 프로젝트 bt-dev-web-01** 의 서비스계정 키(tts_key.json)로
      Cloud Text-to-Speech 를 직접 호출한다. 두 신원이 공존한다.

장점 vs 구 Gemini-TTS:
    - 언어별 네이티브 음성(ko/ja/en/zh/fr/vi Chirp3-HD) + 통화 캐릭터 음색(voice) — **언어·음색
      두 축**. 일본어 문장을 한국어 발음으로 읽던 버그를 해소하면서 통화 목소리를 그대로 유지.
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

# 언어(ISO 코드) → (BCP-47 languageCode, 기본 Chirp3-HD 음성명). 미지원 언어는 ko 폴백.
# 6개 언어 모두 Chirp3-HD 30종 확인(2026-07). 기본값(Aoede)은 캐릭터 voice 가 없을 때만 쓴다
# (아래 synthesize(voice=...) 로 캐릭터 음색 우선).
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

# 프리빌트 음성 30종. **로스터(이름 목록)는 Gemini Live 캐릭터 voice·Gemini-TTS 와 같다** —
# 통화 캐릭터의 voice(예: "Fenrir")가 그대로 이 목록에 있다.
# ⛔ 그런데 **API 가 요구하는 문자열 형식은 엔진마다 다르다**(2026-08-07 실사격으로 확인):
#     Chirp3-HD   : 'ko-KR-Chirp3-HD-Sulafat'   (언어·계열 접두어)
#     Gemini-TTS  : 'Sulafat'                   (맨이름)
#   섞으면 400 "Gemini models cannot be used with non-Gemini voices." / 404 Voice not found 다.
#   그래서 로스터는 공유하되(오타 방어) **문자열은 엔진별로 만든다**(_resolve_voice).
#
# ⭐ 오늘 같은 함정을 **세 번** 밟았다. "같은 구글이니 같은 규칙일 것"이 세 번 다 틀렸다:
#     ① LINEAR16   — 비스트리밍엔 유효, 스트리밍엔 무효(400)
#     ② 모델 ID     — Cloud TTS 의 model_name 과 Gemini API 모델 ID 가 다른 문자열
#     ③ 음성명 형식 — 위 두 줄
#   다음 사람에게: **문서로 같아 보여도 한 번 쏴보고 확정해라.** 셋 다 실사격에서만 드러났다.
_CHIRP3_HD_VOICES: frozenset[str] = frozenset({
    "Achernar", "Achird", "Algenib", "Algieba", "Alnilam", "Aoede", "Autonoe",
    "Callirrhoe", "Charon", "Despina", "Enceladus", "Erinome", "Fenrir", "Gacrux",
    "Iapetus", "Kore", "Laomedeia", "Leda", "Orus", "Puck", "Pulcherrima",
    "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar", "Sulafat", "Umbriel",
    "Vindemiatrix", "Zephyr", "Zubenelgenubi",
})


def _resolve_voice(
    language: str, voice: str | None = None, gemini: bool = False
) -> tuple[str, str]:
    """(languageCode, voice_name) 반환. **엔진에 따라 이름 형식이 다르다.**

    언어(코드 'ja' 또는 라벨 '일본어')로 언어를 정하고, voice(캐릭터 음색 이름)가 로스터에
    있으면 그 목소리를, 아니면 언어 기본 음성을 쓴다. 미상 언어는 ko 폴백.

    gemini=True 면 **맨이름**('Sulafat')을 돌려준다. Gemini-TTS 는 접두어가 붙은 이름을
    거절한다(400 "Gemini models cannot be used with non-Gemini voices."). 기본값(False)은
    Chirp3-HD 형식('ko-KR-Chirp3-HD-Sulafat') 그대로 — ⛔ 비스트리밍 synthesize()(표현
    오디오)가 이 경로를 쓰므로 **기본 동작을 바꾸면 안 된다.**
    """
    code = _LABEL_TO_CODE.get((language or "").strip(), (language or "ko").strip().lower())
    lang_code, default_name = _VOICE_BY_LANG.get(code, _VOICE_BY_LANG["ko"])
    picked = voice.strip() if voice and voice.strip() in _CHIRP3_HD_VOICES else ""
    if gemini:
        # 로스터에 없으면 언어 기본 음성의 맨이름으로 떨어진다('ko-KR-Chirp3-HD-Aoede' → 'Aoede').
        return lang_code, picked or default_name.rsplit("-", 1)[-1]
    if picked:
        return lang_code, f"{lang_code}-Chirp3-HD-{picked}"
    return lang_code, default_name


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


# 엔진 이름 = **원가 벤더 문자열이기도 하다**(normalcall_service._tts_cost_usd 의
# 단가표 키와 정확히 같아야 원가가 0 으로 안 떨어진다).
CHIRP3_ENGINE = "cloud-tts-chirp3-hd"
GEMINI_ENGINE = "gemini-tts"  # `synthesize(engine=...)` 가 받는 값(벤더 이름 아님)


async def synthesize(
    text: str,
    language: str = "ko",
    voice: str | None = None,
    engine: str | None = None,
) -> tuple[bytes, str] | None:
    """텍스트를 대상 언어 음성으로 합성 → (mp3_bytes, "audio/mpeg") 또는 None.

    (멀티랭귀지) language 는 ISO 코드("ja") 또는 라벨("일본어") — 일본어 문장은 일본어 음성으로.
    voice: 통화 캐릭터의 Gemini Live voice 이름(예: "Fenrir"). 유효하면 그 목소리로, 없거나
    미지원이면 언어 기본 음성으로 합성한다. Cloud TTS 는 MP3 를 직접 주므로 ffmpeg 불필요.
    키 부재/합성 실패는 None(graceful) — 호출부는 None 이면 TTS 를 건너뛴다.

    engine: `None`(기본) 이면 **Chirp3-HD** 다 — 기존 호출처(표현 오디오·힌트)의 동작을
    그대로 둔다. `"gemini-tts"` 면 Gemini-TTS 모델로 합성한다(취약 발음 학습이 이걸 쓴다).

    ⛔ 엔진마다 **음성명 형식이 다르다** — Chirp3 는 `ko-KR-Chirp3-HD-Sulafat`,
      Gemini 는 맨이름 `Sulafat` 이다. 섞으면 400 "Gemini models cannot be used with
      non-Gemini voices." 가 난다. 그래서 `_resolve_voice(gemini=...)` 로 갈라 받는다.
    ⚠ Gemini 경로가 실패하면 **Chirp3 로 한 번 더 시도**한다. 소리가 아예 안 나는 것보다
      다른 목소리로라도 나는 편이 낫다(스트리밍 경로의 폴백 규율과 같다).
    """
    if not text or not text.strip():
        return None
    cli = _client()
    if cli is None:
        return None
    want_gemini = (engine or "").strip() == GEMINI_ENGINE
    if want_gemini:
        model = (settings.CASCADE_TTS_GEMINI_MODEL or "").strip()
        if model:
            got = await _synthesize_once(cli, text, language, voice, model)
            if got is not None:
                return got
            logger.warning("tts(gcp): gemini-tts 실패 → chirp3-hd 로 폴백.")
        else:
            logger.warning("tts(gcp): CASCADE_TTS_GEMINI_MODEL 미설정 → chirp3-hd 로 합성.")
    return await _synthesize_once(cli, text, language, voice, None)


async def _synthesize_once(
    cli: "Any",
    text: str,
    language: str,
    voice: str | None,
    model_name: str | None,
) -> tuple[bytes, str] | None:
    """엔진 하나로 MP3 한 번 합성. 실패는 None(호출부가 폴백을 정한다)."""
    lang_code, voice_name = _resolve_voice(language, voice, gemini=bool(model_name))
    try:
        from google.cloud import texttospeech

        voice_kwargs: dict = {"language_code": lang_code, "name": voice_name}
        if model_name:
            voice_kwargs["model_name"] = model_name
        resp = await cli.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text.strip()),
            voice=texttospeech.VoiceSelectionParams(**voice_kwargs),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3
            ),
        )
        if not resp.audio_content:
            logger.warning("tts(gcp): 합성 결과 비어있음 → None.")
            return None
        logger.info("tts(gcp): 합성 성공 MP3(%d bytes, voice=%s, model=%s).",
                    len(resp.audio_content), voice_name, model_name or "chirp3-hd")
        return resp.audio_content, "audio/mpeg"
    except Exception as exc:  # noqa: BLE001 - 인증/비활성/임의 예외 graceful
        logger.warning("tts(gcp): 합성 실패(무시, None) — %s", exc)
        return None
