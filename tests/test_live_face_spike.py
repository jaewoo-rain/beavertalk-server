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


def test_the_face_tool_is_blocking_and_matches_the_client_vocabulary():
    """⛔⛔ **behavior 를 지정하지 않는다 = 기본 블로킹**(2026-08-20 A안).

    공식문서: "Function calling executes **sequentially by default**, meaning execution
    pauses until the results of each function call are available." ⇒ 기본이 표준 경로다.

    여기 있던 `NON_BLOCKING` 은 **쓰이지도 않는 LEVELTEST_DONE_TOOL 에서 복사해 온 것**
    이었고, 그것이 실패 셋을 전부 만들었다:
      · SILENT    → "생성하지 마"로 답한 셈 ⇒ 4턴 내내 대답 0건
      · WHEN_IDLE → 턴 사이엔 할 일이 없어 즉시 재개 ⇒ 32초에 89회 폭주
      · INTERRUPT → 하던 말을 자른다(미시도)
    세 값 다 "모델은 기다리는데 우리는 안 기다린다고 선언한 상태"의 증상이다.

    ⛔ 이 단언을 NON_BLOCKING 으로 되돌리려거든 위 문서 인용부터 반박해라. 그리고
      되돌린다면 send_tool_response 의 scheduling 분기도 **같이** 살려야 한다 —
      한쪽만 고치면 증상이 위 셋 중 하나로 정확히 재발한다.

    그리고 값 집합은 **클라 아바타 어휘 5종**과 같아야 한다 — 커밋 d1139d8 이
    "매핑 계층을 만들지 않는다"로 정한 축이다. 여기가 갈리면 서버가 보낸 표정을
    클라가 조용히 neutral 로 떨어뜨린다.
    """
    fn = SET_FACE_TOOL.function_declarations[0]
    assert fn.name == "set_face"
    assert fn.behavior is None, "표정 tool 은 기본(블로킹)이어야 한다"
    assert set(fn.parameters.properties["emotion"].enum) == {
        "neutral", "happy", "surprised", "sad", "angry"
    }


def test_live_config_without_tools_is_unchanged():
    """⛔ 스파이크가 꺼진 통화의 config 에는 tools 가 **없다**(None)."""
    assert build_live_config(system_instruction="x", voice="Leda").tools is None
    assert build_live_config(
        system_instruction="x", voice="Leda", tools=[SET_FACE_TOOL]
    ).tools == [SET_FACE_TOOL]


def test_the_face_response_omits_scheduling_but_leveltest_stays_silent():
    """⛔⛔ **이걸 틀리면 비버가 말을 안 한다**(2026-08-18 실측으로 배웠다).

    `SILENT` = "맥락에만 넣고 **생성을 트리거하지 않는다**".
    레벨테스트는 그게 맞다 — 그 호출의 목적이 **통화를 끝내는 것**이라 이어 말할 필요가 없다.
    표정은 정반대다: 부르고 **계속 말해야** 한다.

    실측: set_face 를 4턴 내내 불렀는데 **첫 턴 말고는 대답이 0건**이었다
    (사장님: "AI가 대답을 안 하는데?"). 모델이 호출 뒤 응답을 기다렸는데 우리가
    "생성하지 마"라고 답한 셈이다.

    ⚠ `INTERRUPT` 는 안 된다 — 하던 말을 자른다.
    """
    import google.genai.types as types

    from core.gemini_live import GeminiLiveSession

    sent = []

    class _FakeSession:
        async def send_tool_response(self, *, function_responses):
            sent.extend(function_responses)

    import asyncio

    live = GeminiLiveSession(_FakeSession())

    asyncio.run(live.send_tool_response("id1", "leveltest_ceiling_reached"))
    assert sent[-1].scheduling == types.FunctionResponseScheduling.SILENT

    # ⛔⛔ 표정은 이제 **blocking** 이다 — scheduling 을 아예 안 붙인다(2026-08-20).
    #   scheduling 은 NON_BLOCKING 응답을 "언제 맥락에 넣고 생성을 촉구할지" 고르는 칸이다.
    #   블로킹에서는 모델이 이미 이 응답을 기다리므로 **도착 자체가 재개**다. 여기에
    #   SILENT/WHEN_IDLE 을 얹으면 기다리는 모델에게 "생성하지 마"/"할 일 없으면 또 해"를
    #   덧대는 꼴이 되고, 그게 위 함수 docstring 이 적은 실패 셋이다.
    asyncio.run(live.send_tool_response("id2", "set_face", blocking=True))
    assert sent[-1].scheduling is None, "블로킹 응답에 scheduling 을 붙이면 안 된다"

    # ⚠ 레벨테스트 경로(blocking=False)는 **바이트 동일**이어야 한다 — 위 SILENT 단언과
    #   이 줄이 그 불변식을 양쪽에서 잡는다.
    asyncio.run(live.send_tool_response("id3", "leveltest_ceiling_reached", resume=True))
    assert sent[-1].scheduling == types.FunctionResponseScheduling.WHEN_IDLE


# ── 마커 프레임(2026-08-19 본편) ─────────────────────────────────────────────
def test_the_marker_carries_no_text_so_the_subtitle_path_is_untouched():
    """⛔ `text` 는 항상 빈 문자열이다.

    프론트 `_fireDueMarkers` 가 `if (m.text.isNotEmpty)` 로 자막을 가른다 ⇒ 비워 보내면
    자막 로직을 아예 안 탄다. Live 자막은 지금처럼 `output_transcript` 로 **먼저** 뜬다
    (2026-08-19 사장님 결정: "자막은 지금처럼 먼저 보여줘야지").
    ⚠ 여기에 텍스트를 채우면 Live 자막이 **두 시계로 갈린다.**
    """
    from domains.learning.realtime.protocol import ServerSentenceMarker

    m = ServerSentenceMarker(seq=1, emotion="happy")
    assert m.text == ""
    assert m.type == "sentence"


def test_the_marker_survives_a_turn_that_has_not_opened_yet():
    """⛔⛔ **턴이 없어도 나가야 한다.**

    모델은 말하기 **전에** 표정을 정한다 — 실측 28호출 중 27회가 그 턴 오디오 **0.00초**
    지점이었다. 그 시점의 `state.turn_id` 는 대개 None 이다. `turn_id` 를 필수로 두면
    프레임이 **거의 전부 사라진다.**

    ⚠ 그렇다고 여기서 턴을 새로 열면 더 나쁘다 — `_forward_event` 의 `turn_started` 가
      영영 False 가 되어 **학습자 발화 확정·통화 시계 시작**이 통째로 건너뛰어진다(R4).
    """
    from domains.learning.realtime.protocol import ServerSentenceMarker

    assert ServerSentenceMarker(seq=1, emotion="sad").turn_id == ""


def test_an_unknown_emotion_is_forwarded_not_dropped():
    """⛔ 화이트리스트로 막지 않는다 — 클라가 모르는 값을 neutral 로 떨어뜨린다.

    ⭐ 그래서 서버가 감정을 늘려도 **앱 배포를 기다릴 필요가 없다**(프론트 `knownLabel` 주석).
    막아 버리면 그 성질이 죽는다.
    """
    from domains.learning.realtime.protocol import ServerSentenceMarker

    assert ServerSentenceMarker(seq=1, emotion="excited").emotion == "excited"


def test_the_marker_is_in_the_live_server_union():
    """와이어 유니온에 들어 있어야 클라 계약 문서·검증이 이 프레임을 안다."""
    import typing

    from domains.learning.realtime.protocol import ServerMessage, ServerSentenceMarker

    members = typing.get_args(typing.get_args(ServerMessage)[0])
    assert ServerSentenceMarker in members
