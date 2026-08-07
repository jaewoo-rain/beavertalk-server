"""캐스케이드 스트리밍 TTS 설정 — **인코딩이 틀리면 소리가 아예 안 난다.**

2026-08-07 실통화 회귀: `LINEAR16` 으로 나가 문장마다 `400 Unsupported audio encoding` 이
떴다. STT·LLM 은 정상이었고 비버만 벙어리였다(첫소리=-1ms). 비스트리밍 synthesize_speech
에서는 LINEAR16 이 유효해서 되돌아가기 쉬운 자리라 테스트로 못박는다.

설치된 SDK 원문(StreamingAudioConfig.audio_encoding):
  "Streaming supports PCM, ALAW, MULAW and OGG_OPUS. All other encodings return an error."
"""

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
