"""core/tts.py `synthesize()` 계약 — 표현 오디오·발음 복습이 쓰는 공용 합성기.

⛔⛔ P2-6(2026-09-24, bt-back QA) — 캐스케이드 통화용 스트리밍 합성
(`synthesize_stream`·`build_streaming_config`)은 캐스케이드 엔진 삭제로 호출부가
0건이라 core/tts.py 에서 통째로 지웠다(그 코드만 시험하던 `test_tts_engine_
switch.py`·`test_tts_streaming_config.py` 도 함께 지웠다). 이 파일은 그중
**살아있는 비스트리밍 경로**를 여전히 지키던 시험 3개만 추려 옮긴 것이다 — 원본
시험 내용은 손대지 않았다(주석·경로만 갱신).
"""

from __future__ import annotations

import inspect

from core import tts


def test_non_streaming_synthesize_stays_mp3_and_non_streaming():
    """⛔ 비스트리밍 경로는 **MP3 · 비스트리밍** 그대로다.

    2026-09-21 에 이 경로에 엔진 선택(`engine=`)이 붙었다 — 취약 발음 학습이 구
    Gemini-TTS 를 쓰기 때문이다. 합성 본체는 `_synthesize_once` 로 떨어져 나갔고,
    `synthesize` 는 엔진을 고르고 폴백하는 껍데기가 됐다.

    그래서 단언 대상을 **비스트리밍 경로 전체**로 넓힌다. 지켜야 할 것은 함수의 모양이
    아니라 계약이다 — 인코딩은 MP3 고, 스트리밍 API 는 타지 않는다.
    """
    shell = inspect.getsource(tts.synthesize)
    body = inspect.getsource(tts._synthesize_once)

    assert "AudioEncoding.MP3" in body
    assert "AudioEncoding.MP3" not in shell

    # 둘 중 어느 쪽도 스트리밍 경로를 타지 않는다.
    for src in (shell, body):
        assert "streaming" not in src.lower()

    # 반환 계약도 그대로다.
    assert '"audio/mpeg"' in body


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


def test_teaching_prompt_still_says_slowly_and_clearly():
    """⛔⛔ **금지 구역** — normalcall 교수법의 "천천히 또박또박"은 지우면 안 된다.

    이 코스법 지시가 TTS 스타일 설정(캐스케이드 스트리밍 전용, 이제 삭제됨)과
    혼동돼 같이 지워진 전례가 있었다. 이건 TTS 목소리가 아니라 **LLM 에게 주는
    교수법**이고, 학습자가 그걸 듣고 따라 말한다("2번 따라 말하게" — 에코 결함도
    이 문장이 근거였다).
    """
    # ⭐ 2026-09-12 잠금/편집 분리 — 공부 절차 문장은 core/prompts/locked/normal.py 로 **이동**했다(persona_prompt 는 import 만).
    from core.prompts.locked import normal as locked_normal

    src = inspect.getsource(locked_normal)
    assert "천천히 또박또박" in src, "교수법 지시가 사라졌다 — TTS 스타일과 혼동한 것이다"
    assert "2번 따라 말하게" in src
