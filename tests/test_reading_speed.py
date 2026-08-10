"""말하기 속도 실측 — **Live 를 정답지로 삼기 위한 공통 잣대**(2026-08-10).

사장님: "라이브에서 10글자를 말하는데 얼마나 걸리는지 보고 그거와 동일한 속도인지 확인하면
될 것 같아. **라이브에서는 속도가 딱 좋아.**"

지금까지 캐스케이드 TTS 가 "느리다"는 건 귀의 판정이었고 **목표 숫자가 없었다.** 내가 "몇
자per초가 정상"이라고 정하면 그건 또 근거 없는 상수다. 사장님이 좋다고 하신 실물(Live)에서
나온 값이 목표가 된다 — 그러려면 **두 경로가 같은 식으로** 재야 한다.

여기서 고정하는 성질:
  ① 바이트→초 환산은 **한 곳**(core.audio)에서 나온다. 두 엔진이 각자 계산하지 않는다
  ② 언어 구분은 레벨테스트가 쓰는 **같은 스크립트 표**로 한다
  ③ 섞인 발화는 `mixed` 로 낸다 — 오디오가 통짜라 언어별 초를 **알 수 없다**(아는 척 금지)
"""

import core.audio as audio
import domains.learning.realtime.call_session as cs_live
import domains.learning.realtime.cascade_session as cs


def _pcm_ms(ms: float) -> int:
    """[ms] 길이의 출력 PCM 바이트 수."""
    return int(ms * audio.OUTPUT_BYTES_PER_MS)


def test_both_engines_share_one_bytes_to_seconds_rule():
    """⛔ **잣대가 하나여야 비교가 성립한다.**

    Live 값이 캐스케이드의 목표가 되는데, 두 경로가 다른 상수를 쓰면 그 목표는 거짓이 된다.
    """
    assert cs.BEAVER_BYTES_PER_MS is audio.OUTPUT_BYTES_PER_MS
    # 24kHz · 16bit · 모노 = 48 bytes/ms (출력 PCM 규격)
    assert audio.OUTPUT_BYTES_PER_MS == 24_000 * 2 / 1000.0
    assert audio.output_audio_s(_pcm_ms(2_500)) == 2.5


def test_output_audio_s_never_breaks_a_call():
    """계측이 통화를 죽이지 않는다(R5) — 음수·잡값은 0 초."""
    assert audio.output_audio_s(-100) == 0.0
    assert audio.output_audio_s("이상한값") == 0.0


def test_live_line_labels_a_korean_utterance():
    """한국어로 쏠린 발화는 `ko` 로 이름표가 붙는다 — 그 값이 캐스케이드 ko 의 목표다."""
    line = cs_live._reading_speed_line("안녕하세요 오늘 기분이 어때요?", _pcm_ms(4_000))
    assert "[ko:" in line and "자per초" in line, line
    assert "/4.0초]" in line, line


def test_live_line_labels_an_english_utterance():
    line = cs_live._reading_speed_line("Hello, how are you today?", _pcm_ms(2_000))
    assert "[en:" in line, line


def test_mixed_utterance_is_not_split_into_fake_per_language_numbers():
    """⛔ 섞인 발화에서 언어별 초를 **지어내지 않는다.**

    오디오는 통짜 하나라 "한국어가 몇 초를 썼는지"를 알 수 없다. 캐스케이드는 마커로 구간을
    갈라 보내므로 알 수 있지만 Live 는 그 재료가 없다. 모르면 모른다고 낸다.
    """
    line = cs_live._reading_speed_line("Hello 안녕하세요 nice to meet you", _pcm_ms(3_000))
    assert "[mixed:" in line, line
    assert "자per초" in line, "전체 값은 그래도 낸다 — 그것만으로도 판단은 된다"


def test_no_audio_is_reported_as_unmeasurable():
    """소리가 없으면 0 자per초가 아니라 '측정불가'다 — 0 은 '아주 느리다'로 읽힌다."""
    assert "측정불가" in cs_live._reading_speed_line("안녕하세요", 0)
    assert "측정불가" in cs_live._reading_speed_line("", _pcm_ms(1_000))


def test_live_logs_reading_speed_once_per_beaver_utterance(caplog):
    """⭐ 비버 발화 1건 = 로그 한 줄. ⛔ 흐름은 안 건드린다(R4) — 있는 값의 길이만 센다."""
    caplog.set_level("INFO")
    state = cs_live._CallState()
    state.cur_beaver_text = ["안녕하세요 ", "오늘은 뭐 했어요?"]
    state.cur_beaver_pcm = bytearray(_pcm_ms(3_000))
    cs_live._flush_beaver_segment(state)
    speed_lines = [m for m in caplog.messages if "읽기=" in m]
    assert len(speed_lines) == 1, caplog.messages
    assert "[ko:" in speed_lines[0]
    # 세그먼트 적재·초기화 같은 기존 동작은 그대로다
    assert state.segments and state.segments[-1]["role"] == "beaver"
    assert state.cur_beaver_pcm == bytearray() and state.cur_beaver_text == []
