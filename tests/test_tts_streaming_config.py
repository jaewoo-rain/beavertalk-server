"""캐스케이드 스트리밍 TTS 설정 — **인코딩이 틀리면 소리가 아예 안 난다.**

2026-08-07 실통화 회귀: `LINEAR16` 으로 나가 문장마다 `400 Unsupported audio encoding` 이
떴다. STT·LLM 은 정상이었고 비버만 벙어리였다(첫소리=-1ms). 비스트리밍 synthesize_speech
에서는 LINEAR16 이 유효해서 되돌아가기 쉬운 자리라 테스트로 못박는다.

설치된 SDK 원문(StreamingAudioConfig.audio_encoding):
  "Streaming supports PCM, ALAW, MULAW and OGG_OPUS. All other encodings return an error."
"""

import inspect

import pytest

from core import tts

texttospeech = pytest.importorskip("google.cloud.texttospeech")


def test_streaming_config_uses_pcm_not_linear16():
    """⛔ 스트리밍은 PCM 이다. LINEAR16 이면 400 이 나고 사장님 통화가 무음이 된다."""
    config = tts.build_streaming_config("ko-KR", "ko-KR-Chirp3-HD-Aoede", 24000)
    encoding = config.streaming_audio_config.audio_encoding
    assert encoding == texttospeech.AudioEncoding.PCM
    assert encoding != texttospeech.AudioEncoding.LINEAR16
    # 스트리밍이 허용하는 값 안에 있는가(원문 그대로의 목록)
    assert encoding in {
        texttospeech.AudioEncoding.PCM,
        texttospeech.AudioEncoding.ALAW,
        texttospeech.AudioEncoding.MULAW,
        texttospeech.AudioEncoding.OGG_OPUS,
    }


def test_streaming_config_keeps_client_playback_contract():
    """서버→클라 오디오 규약(PCM16 24kHz)이 그대로여야 앱 재생 경로에 꽂힌다."""
    config = tts.build_streaming_config("ko-KR", "ko-KR-Chirp3-HD-Aoede", tts.CASCADE_TTS_SAMPLE_RATE)
    assert tts.CASCADE_TTS_SAMPLE_RATE == 24000
    assert config.streaming_audio_config.sample_rate_hertz == 24000
    assert config.voice.name == "ko-KR-Chirp3-HD-Aoede"
    assert config.voice.language_code == "ko-KR"


def test_non_streaming_synthesize_is_untouched():
    """⛔ 비스트리밍 경로(통화후 분석의 표현 오디오)는 MP3 그대로다 — 여기 손대면 안 된다."""
    import inspect

    source = inspect.getsource(tts.synthesize)
    assert "AudioEncoding.MP3" in source
    assert "streaming" not in source.lower()


def test_voice_name_format_differs_per_engine():
    """⛔ 로스터는 같지만 **문자열 형식이 다르다**(2026-08-07 실사격에서만 드러났다).

        Chirp3-HD  : 'ko-KR-Chirp3-HD-Sulafat'  (언어·계열 접두어)
        Gemini-TTS : 'Sulafat'                  (맨이름)
    섞으면 400 "Gemini models cannot be used with non-Gemini voices." / 404 Voice not found.
    """
    assert tts._resolve_voice("ko", "Sulafat") == ("ko-KR", "ko-KR-Chirp3-HD-Sulafat")
    assert tts._resolve_voice("ko", "Sulafat", gemini=True) == ("ko-KR", "Sulafat")
    # 로스터에 없는 이름은 양쪽 다 언어 기본 음성으로 떨어진다(오타 방어는 유지).
    assert tts._resolve_voice("ko", "없는목소리") == ("ko-KR", "ko-KR-Chirp3-HD-Aoede")
    assert tts._resolve_voice("ko", "없는목소리", gemini=True) == ("ko-KR", "Aoede")
    # 언어가 바뀌어도 규칙은 같다(일본어 Chirp 접두어 vs 맨이름).
    assert tts._resolve_voice("ja", "Leda") == ("ja-JP", "ja-JP-Chirp3-HD-Leda")
    assert tts._resolve_voice("ja", "Leda", gemini=True) == ("ja-JP", "Leda")


def test_speaking_rate_is_a_parameter_not_a_request():
    """⭐ 말하는 속도는 **파라미터**로 잡는다 — 스타일 프롬프트로 부탁하면 편차가 1.5배였다.

    실측(2026-08-07): 2.4 · 2.8 · 6.7 · 8.4 · 10.0 자/초. 같은 지시문인데도 들쭉날쭉했다.
    proto 원문: "in the range [0.25, 2.0]. 1.0 is the normal native speed".
    """
    config = tts.build_streaming_config("ko-KR", "Sulafat", 24000, speaking_rate=1.25)
    assert config.streaming_audio_config.speaking_rate == pytest.approx(1.25)


def test_normal_speed_changes_nothing():
    """⛔ 기본값(1.0)에서는 필드를 아예 안 넘긴다 — 배포만으로는 아무것도 안 바뀌어야 한다."""
    config = tts.build_streaming_config("ko-KR", "Sulafat", 24000, speaking_rate=1.0)
    assert config.streaming_audio_config.speaking_rate == 0.0   # proto 기본 = 미설정
    same = tts.build_streaming_config("ko-KR", "Sulafat", 24000)
    assert config.streaming_audio_config == same.streaming_audio_config


def test_out_of_range_is_clamped_not_rejected():
    """범위를 벗어나면 API 가 거절해 그 문장이 통째로 무음이 된다 — 잘라서 소리를 낸다."""
    fast = tts.build_streaming_config("ko-KR", "Sulafat", 24000, speaking_rate=9.9)
    slow = tts.build_streaming_config("ko-KR", "Sulafat", 24000, speaking_rate=0.01)
    assert fast.streaming_audio_config.speaking_rate == pytest.approx(2.0)
    assert slow.streaming_audio_config.speaking_rate == pytest.approx(0.25)


def test_speaking_rate_applies_to_both_engines():
    """엔진 공통 필드다 — Gemini 든 Chirp 든 같은 손잡이가 걸린다."""
    gemini = tts.build_streaming_config(
        "ko-KR", "Sulafat", 24000, model_name="gemini-2.5-flash-tts", speaking_rate=1.4
    )
    chirp = tts.build_streaming_config("ko-KR", "ko-KR-Chirp3-HD-Sulafat", 24000, speaking_rate=1.4)
    assert gemini.streaming_audio_config.speaking_rate == pytest.approx(1.4)
    assert chirp.streaming_audio_config.speaking_rate == pytest.approx(1.4)


def test_prompts_do_not_dictate_speed():
    """⚠ 프롬프트가 속도를 얘기하면 파라미터와 싸운다 — 어느 게 진짜인지 못 가리게 된다.

    캐스케이드가 쓰는 **두 문구**(TTS 스타일 / 레벨 프로파일)에서 속도 지시를 뺐다.
    ⛔ normalcall 의 교수법 지시("천천히 또박또박 들려주고 따라 말하게")는 **건드리지 않았다** —
      그건 실서비스의 어학적 의도이고 Live 는 모델이 직접 음성을 낸다(아래 전용 테스트가 지킨다).
    """
    from core.config import settings

    for prompt in (settings.CASCADE_TTS_STYLE_PROMPT, settings.CASCADE_PERSONA_LEVEL):
        for word in ("천천히", "빠르게", "속도"):
            assert word not in prompt, prompt


def test_tts_style_prompt_has_no_pacing_words():
    """⭐ TTS 스타일 프롬프트에 **말 빠르기를 건드리는 어휘가 없어야 한다.**

    같은 실수가 두 번 났다 — "천천히"를 지우면서 **같은 뜻인 "또박또박"을 남겼다.** 사람이
    기억으로 막을 일이 아니라서 성질로 박는다.
    실측 근거(2026-08-10): Gemini-TTS 가 한국어를 **1.3자per초**로 읽었다(Chirp 4.5~5.6).
    스타일 프롬프트는 **Gemini 에만** 간다 — Chirp 가지는 빈 문자열을 넘긴다.
    ⛔ 속도는 프롬프트가 아니라 **`speaking_rate` 파라미터**가 맡는다. 둘 다 건드리면 어느 게
      진짜인지 못 가린다.
    """
    from core.config import settings

    banned = ("천천히", "또박또박", "느리게", "느릿", "차분", "slow", "slowly", "pace")
    text = settings.CASCADE_TTS_STYLE_PROMPT.lower()
    for word in banned:
        assert word not in text, (word, settings.CASCADE_TTS_STYLE_PROMPT)
    # ⚠ 감정·톤은 남아야 한다 — 사장님이 원하시는 건 표현력이지 무미건조함이 아니다.
    assert settings.CASCADE_TTS_STYLE_PROMPT.strip(), "스타일을 통째로 비우지는 않는다"


def test_teaching_prompt_still_says_slowly_and_clearly():
    """⛔⛔ **금지 구역** — normalcall 교수법의 "천천히 또박또박"은 지우면 안 된다.

    TTS 스타일에서 같은 낱말을 뺐다고 `grep` 으로 여기까지 지우면 **가르치는 방식이 통째로
    무너진다.** 이건 TTS 목소리가 아니라 **LLM 에게 주는 교수법**이고, 학습자가 그걸 듣고 따라
    말한다("2번 따라 말하게" — 오늘 고친 에코 결함도 이 문장이 근거였다).
    """
    import core.persona_prompt as pp

    src = inspect.getsource(pp)
    assert "천천히 또박또박" in src, "교수법 지시가 사라졌다 — TTS 스타일과 혼동한 것이다"
    assert "2번 따라 말하게" in src
