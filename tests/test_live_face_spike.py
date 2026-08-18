"""라이브 표정 계측 스파이크 — **꺼져 있으면 아무것도 안 바뀐다**를 못박는다.

⛔ 이 스파이크는 기능이 아니다. 목적은 통화 한 번으로 미지수 셋을 답하는 것이다:
    ① 모델이 `set_face` 를 부르기는 하는가   ← `live_tools` 첫 실사용이라 아무도 모른다
    ② 부른다면 그 문장 오디오보다 **먼저** 오는가
    ③ 얼마나 자주 부르는가                    ← 깜빡임 억제값을 정한다
①이 아니면 ②③은 의미가 없다. 그래서 화면을 바꾸는 코드는 **일부러 안 넣었다**.

⚠ 그리고 이 스파이크는 Live 세션 config 를 건드린다 — 이 프로젝트에서 제일 조심하는
  자리다. 그래서 "꺼짐 = 무변화"를 값이 아니라 **회귀로** 지킨다.
"""

import core.persona_prompt as pp
from core.gemini_live import SET_FACE_TOOL, build_live_config


_BASE = dict(
    role="비버 선생님", personality="다정하다", level_profile="L1",
    locale="en", interests=["여행"],
)


def test_spike_off_keeps_the_prompt_byte_identical():
    """⛔ 기본값(꺼짐)에서 지시문이 **한 바이트도** 안 바뀐다.

    face_tool 은 옵트인이다. 이게 깨지면 모든 통화의 프롬프트가 조용히 바뀐 것이다 —
    이 프로젝트가 `emotion_tags`·`language_marker` 에 같은 규율을 건 이유와 같다.
    """
    assert pp.build_system_instruction(**_BASE) == pp.build_system_instruction(
        **_BASE, face_tool=False
    )


def test_spike_on_adds_the_face_block_and_only_that():
    """켜면 표정 블록이 **덧붙기만** 한다(기존 문구를 지우거나 바꾸지 않는다)."""
    off = pp.build_system_instruction(**_BASE)
    on = pp.build_system_instruction(**_BASE, face_tool=True)
    assert on != off
    assert off in on, "기존 지시문이 보존되지 않았다 — 덧붙이기가 아니다"
    assert "set_face" in on


def test_the_face_rule_never_asks_for_a_spoken_tag():
    """⛔ **텍스트 태그를 시키면 안 된다.** native-audio 는 쓰는 족족 낭독한다.

    `_EMOTION_TAG_RULE`(캐스케이드용)이 Live 에 금지된 이유가 그것이고, 이 규칙이
    그 함정을 다시 밟으면 비버가 "꺾쇠 해피"라고 말한다.
    """
    rule = pp._FACE_TOOL_RULE
    assert "<" not in rule.replace("<꺾쇠>", ""), rule
    assert "소리 내어 읽지 마라" in rule


def test_the_face_tool_is_non_blocking_and_matches_the_client_vocabulary():
    """⭐ NON_BLOCKING 이어야 호출이 발화를 안 끊는다.

    그리고 값 집합은 **클라 아바타 어휘 5종**과 같아야 한다 — 커밋 d1139d8 이
    "매핑 계층을 만들지 않는다"로 정한 축이다. 여기가 갈리면 서버가 보낸 표정을
    클라가 조용히 neutral 로 떨어뜨린다.
    """
    import google.genai.types as types

    fn = SET_FACE_TOOL.function_declarations[0]
    assert fn.name == "set_face"
    assert fn.behavior == types.Behavior.NON_BLOCKING
    assert set(fn.parameters.properties["emotion"].enum) == {
        "neutral", "happy", "surprised", "sad", "angry"
    }


def test_live_config_without_tools_is_unchanged():
    """⛔ 스파이크가 꺼진 통화의 config 에는 tools 가 **없다**(None)."""
    assert build_live_config(system_instruction="x", voice="Leda").tools is None
    assert build_live_config(
        system_instruction="x", voice="Leda", tools=[SET_FACE_TOOL]
    ).tools == [SET_FACE_TOOL]
