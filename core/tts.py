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

# Chirp3-HD 프리빌트 음성 30종. 이름이 Gemini Live 캐릭터 voice 와 동일 —
# 통화 캐릭터의 voice(예: "Fenrir")를 그대로 넘기면 표현 오디오가 같은 목소리로 난다.
_CHIRP3_HD_VOICES: frozenset[str] = frozenset({
    "Achernar", "Achird", "Algenib", "Algieba", "Alnilam", "Aoede", "Autonoe",
    "Callirrhoe", "Charon", "Despina", "Enceladus", "Erinome", "Fenrir", "Gacrux",
    "Iapetus", "Kore", "Laomedeia", "Leda", "Orus", "Puck", "Pulcherrima",
    "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar", "Sulafat", "Umbriel",
    "Vindemiatrix", "Zephyr", "Zubenelgenubi",
})


def _resolve_voice(language: str, voice: str | None = None) -> tuple[str, str]:
    """(languageCode, voice_name) 반환.

    언어(코드 'ja' 또는 라벨 '일본어')로 언어를 정하고, voice(캐릭터 Chirp3-HD 이름)가
    유효하면 그 목소리(캐릭터 음색)를, 아니면 언어 기본 음성을 쓴다. 미상 언어는 ko 폴백.
    """
    code = _LABEL_TO_CODE.get((language or "").strip(), (language or "ko").strip().lower())
    lang_code, default_name = _VOICE_BY_LANG.get(code, _VOICE_BY_LANG["ko"])
    if voice and voice.strip() in _CHIRP3_HD_VOICES:
        return lang_code, f"{lang_code}-Chirp3-HD-{voice.strip()}"
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


async def synthesize(
    text: str, language: str = "ko", voice: str | None = None
) -> tuple[bytes, str] | None:
    """텍스트를 대상 언어 Chirp3-HD 음성으로 합성 → (mp3_bytes, "audio/mpeg") 또는 None.

    (멀티랭귀지) language 는 ISO 코드("ja") 또는 라벨("일본어") — 일본어 문장은 일본어 음성으로.
    voice: 통화 캐릭터의 Gemini Live voice 이름(예: "Fenrir"). 유효하면 그 목소리로, 없거나
    미지원이면 언어 기본 음성으로 합성한다. Cloud TTS 는 MP3 를 직접 주므로 ffmpeg 불필요.
    키 부재/합성 실패는 None(graceful) — 호출부는 None 이면 TTS 를 건너뛴다.
    """
    if not text or not text.strip():
        return None
    cli = _client()
    if cli is None:
        return None
    lang_code, voice_name = _resolve_voice(language, voice)
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


# --------------------------------------------------------------------------- #
# 캐스케이드 통화용 스트리밍 합성 (P1) — ⛔ 위 synthesize() 는 한 줄도 건드리지 않는다
# --------------------------------------------------------------------------- #
# 왜 따로 두나: 위 함수는 통화후 분석의 **표현 오디오**(MP3 단발)다. 통화 중 비버 발화는
# 성질이 다르다 — 문장이 끝나기 전에 소리가 나기 시작해야 하고(첫 소리 지연), 포맷은 앱
# 재생 경로에 그대로 꽂히는 **PCM16/24kHz** 여야 하며, 끊겼을 때 안 보낸 문장은 과금되지
# 않아야 한다. 그래서 streaming_synthesize 로 별도 경로를 만든다.
CASCADE_TTS_SAMPLE_RATE = 24000     # 서버→클라 오디오 규약(PCM16 24k mono)
_WAV_HEADER = b"RIFF"
_FIRST_BYTES_LOGGED: set[str] = set()   # 첫 조각 형태를 엔진당 한 번만 남긴다(_log_first_bytes)
# 엔진 이름 = **원가 벤더 문자열이기도 하다.** 단가표(normalcall_service)의 키와 정확히
# 같아야 원가가 0 으로 떨어지지 않는다. gemini 쪽은 모델 id 를 그대로 벤더로 쓴다 —
# flash/lite/pro 단가가 다르므로 'gemini-tts' 로 뭉개면 원가를 못 가른다.
CHIRP3_ENGINE = "cloud-tts-chirp3-hd"
GEMINI_ENGINE = "gemini-tts"            # CASCADE_TTS_ENGINE 이 고르는 값(벤더 이름 아님)


async def synthesize_stream(
    text: str,
    *,
    language: str = "ko",
    voice: str | None = None,
    sample_rate: int = CASCADE_TTS_SAMPLE_RATE,
    report: dict | None = None,
) -> "Any":
    """문장 하나를 PCM16/24k 조각으로 흘린다(async generator). 키 부재·실패면 **아무것도 안 낸다**.

    호출부(캐스케이드)는 조각을 받는 즉시 페이서를 통해 송출한다. 실패해도 예외를 올리지
    않는다 — 비버가 한 문장 못 말해도 통화는 계속돼야 한다(R5).

    ⚠ 응답에 사용량 필드가 없다(SDK 정의 확인 — audio_content 뿐). 과금 문자 수는 호출부가
      **API 에 넘긴 문자열**로 센다(원가 설계 §1-2).

    엔진은 `CASCADE_TTS_ENGINE` 이 정한다("chirp3-hd" | "gemini-tts"). gemini 경로가 실패하면
    **Chirp3-HD 로 폴백하고 WARNING 을 남긴다** — 조용한 폴백은 A/B 비교를 거짓말로 만든다.
    report(dict)를 주면 실제로 소리를 낸 엔진(`engine`)과 폴백 여부(`fallback_from`)를 채워
    준다. 호출부는 그걸로 로그·원가 벤더를 정한다.
    """
    async def _empty():
        return
        yield b""  # pragma: no cover - 타입만 맞추는 빈 제너레이터

    if not text or not text.strip():
        return _empty()
    cli = _client()
    if cli is None:
        return _empty()
    lang_code, voice_name = _resolve_voice(language, voice)

    engine = (settings.CASCADE_TTS_ENGINE or CHIRP3_ENGINE).strip()

    async def _run():
        from google.cloud import texttospeech

        async def _one(model_name: str | None, style_prompt: str, label: str):
            """엔진 하나로 한 문장을 흘린다. (첫 조각을 냈는지, 실패 사유) 를 남긴다."""
            config = build_streaming_config(
                lang_code, voice_name, sample_rate, model_name=model_name
            )

            async def _requests():
                # 첫 요청 = 설정, 이후 = 텍스트. 문장 단위로 열고 닫는다(스트림 1개 = 문장 1개).
                yield texttospeech.StreamingSynthesizeRequest(streaming_config=config)
                kwargs = {"text": text.strip()}
                if style_prompt:
                    # Gemini-TTS 의 감정 지시(Style Instructions). Chirp3-HD 엔 없는 기능이라
                    # 그쪽 경로에서는 넣지 않는다(넣으면 요청이 거절될 수 있다).
                    kwargs["prompt"] = style_prompt
                yield texttospeech.StreamingSynthesizeRequest(
                    input=texttospeech.StreamingSynthesisInput(**kwargs)
                )

            responses = await cli.streaming_synthesize(requests=_requests())
            first = True
            async for resp in responses:
                chunk = getattr(resp, "audio_content", b"") or b""
                if not chunk:
                    continue
                if first:
                    first = False
                    if report is not None:
                        report["engine"] = label
                    _log_first_bytes(chunk, label)
                    # PCM 은 원래 헤더가 없다. 그래도 방어는 남긴다 — 헤더가 섞여 들어오면
                    # 재생 큐에서 딸깍 소리가 나고, 그건 원인 찾기가 오래 걸리는 증상이다.
                    if chunk[:4] == _WAV_HEADER:
                        idx = chunk.find(b"data")
                        chunk = chunk[idx + 8 :] if idx >= 0 else chunk
                        if not chunk:
                            continue
                yield chunk

        if engine == GEMINI_ENGINE:
            model = (settings.CASCADE_TTS_GEMINI_MODEL or "").strip()
            produced = False
            try:
                async for chunk in _one(model, settings.CASCADE_TTS_STYLE_PROMPT, model):
                    produced = True
                    yield chunk
                return
            except Exception as exc:  # noqa: BLE001 - 실패는 폴백으로(R5)
                if produced:
                    # 소리가 이미 나가던 중이면 폴백하지 않는다 — 같은 문장을 두 번 말하게 된다.
                    logger.warning("tts: %s 스트림이 도중에 끊겼다(그 문장은 여기까지) — %s",
                                   model, exc)
                    return
                # ⛔ 조용히 죽지 않는다. 폴백이 조용하면 "제미나이가 느리다"가 아니라
                #   "제미나이인 줄 알았는데 Chirp 였다"가 된다(오늘 400 무음 전례).
                logger.warning(
                    "tts: %s 실패 → Chirp3-HD 로 폴백한다(엔진 비교 로그를 이 줄로 보정해라) — %s",
                    model, exc,
                )
                if report is not None:
                    report["fallback_from"] = model

        try:
            async for chunk in _one(None, "", CHIRP3_ENGINE):
                yield chunk
        except Exception as exc:  # noqa: BLE001 - 합성 실패 graceful(R5)
            logger.warning("tts(gcp): 스트리밍 합성 실패(무시) — %s", exc)

    return _run()


def build_streaming_config(
    lang_code: str, voice_name: str, sample_rate: int, model_name: str | None = None
) -> "Any":
    """스트리밍 합성 설정 1건. **인코딩이 여기서 틀리면 소리가 아예 안 난다.**

    ⛔ `LINEAR16` 을 쓰지 마라. 설치된 SDK 원문(StreamingAudioConfig.audio_encoding):
        "Required. The format of the audio byte stream. Streaming supports PCM, ALAW,
         MULAW and OGG_OPUS. All other encodings return an error."
      2026-08-07 실통화에서 LINEAR16 으로 나가 문장마다 `400 Unsupported audio encoding`
      이 떴다 — STT·LLM 은 정상이었고 소리만 안 났다. 비스트리밍 synthesize_speech 에서는
      LINEAR16 이 유효해서 헷갈리는 자리다(그쪽은 건드리지 않는다).

    PCM = 헤더 없는 리틀엔디언 16bit 샘플이라, 24kHz 로 받으면 그대로 앱 재생 경로에 꽂힌다.
    함수로 떼어낸 이유는 **테스트가 인코딩을 못박기 위해서**다(실통화에서만 드러난 결함이라
    테스트가 없으면 다음에 또 LINEAR16 으로 돌아간다).
    """
    from google.cloud import texttospeech

    # model_name 은 Gemini-TTS 를 고를 때만 넣는다(설치된 SDK 의 VoiceSelectionParams 필드로
    # 확인 — language_code/name/ssml_gender/custom_voice/voice_clone/**model_name**/…).
    # 음색 이름 체계가 Chirp3-HD 와 같아서 **재매핑이 없다**(core/tts.py 상단 주석 참조).
    voice_kwargs: dict = {"language_code": lang_code, "name": voice_name}
    if model_name:
        voice_kwargs["model_name"] = model_name
    return texttospeech.StreamingSynthesizeConfig(
        voice=texttospeech.VoiceSelectionParams(**voice_kwargs),
        streaming_audio_config=texttospeech.StreamingAudioConfig(
            audio_encoding=texttospeech.AudioEncoding.PCM,
            sample_rate_hertz=sample_rate,
        ),
    )


def _log_first_bytes(chunk: bytes, engine: str) -> None:
    """첫 조각의 앞 8바이트를 **엔진마다 한 번씩** 남긴다.

    PCM 에 헤더가 붙어 오는지를 로그로 답하기 위한 것이다(크레덴셜이 없는 워크트리에서는
    확인할 방법이 없다). Chirp3-HD 는 `01 00 00 00 …`(RIFF 아님 = raw PCM)로 확인됐고,
    Gemini-TTS 도 같은지는 **첫 통화 로그 한 줄**로 갈린다. 엔진당 한 번만 찍는다.
    """
    if engine in _FIRST_BYTES_LOGGED:
        return
    _FIRST_BYTES_LOGGED.add(engine)
    logger.info(
        "tts: 스트리밍 첫 조각 engine=%s 앞 8바이트=%s (RIFF 면 헤더 포함, 아니면 raw PCM)",
        engine, chunk[:8].hex(" "),
    )
