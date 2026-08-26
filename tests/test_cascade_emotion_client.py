"""감정을 **클라 아바타 표정 축**으로 맞추고 `turn_start` 에 싣는다(2026-08-12).

사장님: "서버 목소리를 6종 말고 **클라이언트 5종으로 매칭**하도록 해줘."
클라 아바타는 표정 5종을 그린다(`avatar_view.dart`): `neutral · happy · surprised · sad · angry`.
우리 6종(칭찬·격려·질문·설명·정정·인사)은 **화행 축**이라 셋이 neutral 로 뭉개지고
sad·surprised·angry 는 영원히 안 떴다. ⇒ **매핑 계층을 만들지 않고** 서버가 그 어휘로 뱉는다.

⭐ 그리고 사장님 정정: "**캐릭터에 따라** 상대방이 틀리면 슬퍼할 수도 있고, 빈정대는
  캐릭터면 angry 가 될 수도 있잖아." ⇒ 5종 **전부 살아 있어야** 하고, 고르는 기준은
  **캐릭터 성격**이다. 감정 문구에 캐릭터 색(다정한 선생님)을 넣으면 그걸 덮어쓴다.

⚠ 2026-08-26: 클라가 `laugh` 를 6번째로 그리기 시작했다(Live 표정 tool). 캐스케이드는
  **따라가지 않았다** — 여기 감정은 LLM 텍스트 태그에서 오고 그 어휘를 늘리는 것은
  별개 판단이다. ⇒ ① 의 관계가 「같다」에서 **「클라가 그릴 수 있는 것만 보낸다」**로
  바뀐다. 그 방향이 지켜야 할 성질이다 — 반대로 클라가 못 그리는 값을 보내면 조용히
  neutral 로 떨어진다.

여기서 고정하는 성질:
  ① 감정은 **클라가 그리는 것의 부분집합**이다(못 그리는 값을 보내지 않는다)
  ② `turn_start` 에 실린다 — ⛔ **Live 출력은 안 바뀐다**
  ③ 모르는 값이 와도 서버가 안 죽는다(화이트리스트로 막지 않는다)
  ④ 감정 문구에 **캐릭터 색이 없다**(캐릭터는 페르소나가 넣는다)
  ⑤ 프롬프트가 **성격을 감정 규약과 함께** 준다(캐릭터마다 다른 선택이 나오는 근거)
"""

import json

import pytest

import domains.learning.realtime.cascade_reply as cr
import domains.learning.realtime.cascade_session as cs
from domains.learning.realtime.cascade_protocol import (
    CascadeTurnStart,
    cascade_server_adapter,
)

# 캐스케이드가 보내는 표정. ⛔ **클라가 그릴 수 있는 것만** 있어야 한다.
#   클라 어휘(`avatar_view.dart`)는 이보다 넓다 — `laugh` 가 Live 전용으로 더 있다.
CASCADE_FACES = ["neutral", "happy", "surprised", "sad", "angry"]

# 클라가 실제로 그리는 전체 어휘. 위 목록은 이것의 **부분집합**이어야 한다.
CLIENT_DRAWABLE = {"neutral", "happy", "surprised", "sad", "angry", "laugh"}


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send_event(self, event: dict) -> None:
        self.events.append(event)

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def receive(self):
        raise AssertionError("쓰지 않는다")


# ── ① 집합 ─────────────────────────────────────────────────────────────────
def test_the_emotion_set_matches_the_client_faces_exactly():
    """⛔ **매핑 계층을 만들지 않는다.** 서버 어휘 = 클라 어휘여야 그게 가능하다."""
    assert list(cr.EMOTION_STYLES) == CASCADE_FACES
    # ⛔ 클라가 못 그리는 값을 보내면 화면에서 조용히 사라진다.
    assert set(cr.EMOTION_STYLES) <= CLIENT_DRAWABLE


def test_no_emotion_is_suppressed_in_the_prompt():
    """⭐ 사장님 정정 — 5종 **전부 살아 있어야** 한다.

    "어학 선생님이 sad·angry 를 쓸 일이 드물다"는 **하나의 페르소나만 가정한 것**이었다.
    츤데레는 빈정대고(angry) 공감형은 안타까워한다(sad) — 캐릭터를 캐릭터답게 만드는 게 그것이다.
    """
    from core.persona_prompt import build_system_instruction

    prompt = build_system_instruction(
        role="r", personality="p", level_profile="l", locale="en", interests=[],
        emotion_tags=tuple(cr.EMOTION_STYLES),
    )
    for face in CASCADE_FACES:
        assert f"<{face}>" in prompt, face
    for banned in ("거의 안 쓴다", "쓰지 마라 — sad", "angry 는 쓰지"):
        assert banned not in prompt, banned


# ── ④⑤ 캐릭터가 정한다 ────────────────────────────────────────────────────
def test_the_style_phrases_carry_no_character_colour():
    """⛔ 감정 문구는 **얇은 층**이다 — 캐릭터 색은 페르소나(role·personality)가 넣는다.

    예전 문구는 "밝고 기쁘게", "친절하게 알려 주는 **선생님**" 처럼 *다정한 선생님 하나*를
    가정했다. 그러면 츤데레 캐릭터도 "친절한 선생님 목소리"로 읽는다.
    """
    banned = ("선생님", "친절", "다정", "따뜻", "반갑")
    for face, phrase in cr.EMOTION_STYLES.items():
        for word in banned:
            assert word not in phrase, (face, phrase, word)


def test_neutral_is_not_a_flat_reading():
    """⚠ 질문·설명·정정이 전부 neutral 로 합쳐진다 — **무표정한 낭독**이 되면 안 된다."""
    neutral = cr.EMOTION_STYLES["neutral"]
    assert neutral.strip()
    assert "평소 말투" in neutral, neutral       # 평소 말투 = 페르소나가 정한 그 캐릭터의 말투


def test_the_prompt_ties_the_choice_to_the_character():
    """⑤ LLM 출력은 못 박으니 **입력**을 박는다 — 성격이 감정 규약과 함께 들어가는지."""
    from core.persona_prompt import build_system_instruction

    prompt = build_system_instruction(
        role="빈정대는 수달", personality="츤데레. 툴툴대지만 챙긴다.",
        level_profile="A1", locale="en", interests=[],
        emotion_tags=tuple(cr.EMOTION_STYLES),
    )
    assert "빈정대는 수달" in prompt and "츤데레" in prompt
    rule = prompt[prompt.index("[표기 규칙 — 감정 태그]"):]
    assert "성격대로" in rule, rule[:400]
    # ⛔ 대사와 어긋나는 감정은 막는다(그게 유일한 제약이다 — 캐릭터 표현은 안 좁힌다).
    assert "어긋나면 안 된다" in rule


# ── ② turn_start ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_turn_start_carries_the_emotion():
    """프론트 요구: **turn_start 한 칸**(별도 프레임이면 순서 보장을 또 따져야 한다)."""
    sink = _Sink()
    session = cs.CascadeSession(sink, object())
    session._reply_emotion = "sad"
    await session._begin_beaver_turn()

    starts = [e for e in sink.events if e.get("type") == "turn_start"]
    assert starts and starts[0]["emotion"] == "sad", sink.events


@pytest.mark.asyncio
async def test_turn_start_without_a_known_emotion_is_still_valid():
    """감정을 아직 모르면 None 으로 보낸다 — 클라가 neutral 로 그린다."""
    sink = _Sink()
    session = cs.CascadeSession(sink, object())
    await session._begin_beaver_turn()
    assert sink.events[0]["emotion"] is None


def test_live_turn_start_is_untouched():
    """⛔⛔ **Live 출력은 한 글자도 안 바뀐다.**

    그래서 캐스케이드만 자기 모델(`CascadeTurnStart`)을 갖는다 — 공용 모델에 필드를 더하면
    Live 가 보내는 JSON 에도 `"emotion":null` 이 붙는다.
    """
    from domains.learning.realtime.protocol import ServerTurnStart, server_adapter

    live = json.loads(server_adapter.dump_json(ServerTurnStart(turn_id="b1")).decode())
    assert live == {"type": "turn_start", "turn_id": "b1"}, live
    assert "emotion" not in live


# ── ③ 모르는 값 ────────────────────────────────────────────────────────────
def test_an_unknown_emotion_is_not_rejected_by_the_server():
    """⛔ 화이트리스트로 막지 마라(프론트 요청) — 서버가 집합을 늘릴 때 **클라 배포를
    기다리지 않기 위해서**다. 클라는 모르는 값을 neutral 로 떨어뜨린다."""
    frame = json.loads(
        cascade_server_adapter.dump_json(
            CascadeTurnStart(turn_id="b1", emotion="excited")
        ).decode()
    )
    assert frame["emotion"] == "excited"


def test_an_unknown_emotion_falls_back_to_the_default_style():
    """서버 쪽 소비(TTS 스타일)는 모르는 값을 **조용히 기본값**으로 흘린다(R5)."""
    session = cs.CascadeSession(_Sink(), object())
    session._reply_emotion = "excited"
    assert cr.emotion_style("excited") is None
    assert session._style_prompt() is None


# ── ⛔ 잘린 태그(길이 상한이 태그 중간을 자른다) ───────────────────────────
def test_a_tag_cut_in_half_never_reaches_the_tts():
    """⛔ 실측 원문: `… fun with Korean today. <happy` — 상한이 태그 중간을 잘랐다.

    지금은 '미완성 문장 버리기'가 우연히 같이 버려 준다. 그 방어가 **유일**하면 언젠가 샌다
    (대괄호 사고와 같은 계열 — 안 지우면 "해피"라고 읽는다).
    """
    assert cr.strip_emotion_tags("오늘 재미있게 해요. <happy").rstrip() == "오늘 재미있게 해요."
    assert cr.strip_emotion_tags("<hap").strip() == ""


def test_a_bracket_in_the_middle_survives():
    """⚠ 끝에 붙은 것만 지운다 — 문장 중간의 `<` 는 대사의 일부일 수 있다."""
    for text in ("3 < 5 라고 말해요.", "이모티콘 <3 좋아요"):
        assert cr.strip_emotion_tags(text) == text
