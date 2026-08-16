"""대답 길이는 **문장 개수**가 정한다 — 2026-08-12 사장님 결정("응답은 1~4문장이야").

## 이 파일이 대체한 것 — `test_cascade_short_reply.py`(삭제)
하루 전 나는 **캐스케이드 전용 프롬프트 문구**("1~2문장")를 넣었다. 근거는 "토큰 상한이
1문장인데 프롬프트가 4문장을 시킨다"였다. 방향이 틀렸다 — **강제 쪽을 프롬프트에 맞추는 것**이
맞았고, 사장님 지적이 그것이었다:

> "애초에 지금 문장 단위로 잘라서 계속 TTS로 보내는 거 아니야?"

맞다. 우리는 이미 문장 단위로 흘린다(`SentenceBuffer`). 그런데 길이는 토큰으로 잘랐다 —
**단위가 둘이라 경계가 안 맞았고**, 그래서 문장 중간에서 끊겨 꼬리를 버렸다
(call 938 b3: 글자=27 인데 꼬리 99자 = 말한 것의 4배 / 짝 측정 n=24쌍에서 잘림 46%).
⇒ 캐스케이드 전용 분기는 **근거를 잃었다**. 지웠다. 갈래가 없으면 갈릴 일도 없다.

## 여기서 고정하는 성질
  ① 문장 N개를 채우면 **남은 생성을 받지 않는다**(스트림을 닫는다)
  ② 상한에서 끊긴 대답은 **미완성 꼬리를 말하지 않는다**(버리는 양은 문장 시작 몇 글자)
  ③ 상한보다 짧게 끝나면 **예전과 완전히 동일**(모델이 스스로 끝내면 아무 일도 안 한다)
  ④ 0 이면 문장 상한 없음 — **되돌릴 길을 남긴다**
  ⑤ 프롬프트 규칙 5의 숫자 == 서버 상한(`test_cascade_reply_length.py` 가 지킨다)
  ⑥ Live 와 캐스케이드가 **같은 길이 규칙**을 쓴다(전용 분기 없음)

⚠ 한계 하나를 적어 둔다: 세는 단위는 `SentenceBuffer` 가 끊는 **그 문장**이다. 그 분할기는
  8자(`_MIN_SENTENCE_CHARS`) 미만에서는 종결부호가 있어도 안 끊는다(운율 보호) — 즉
  "네. 좋아요. 그럼요." 같은 아주 짧은 말들은 **한 문장으로 셀 수 있다.** 상한이 실제 문장
  수보다 관대해질 수 있다는 뜻이고, 그건 잘림을 만들지 않으므로 이대로 둔다.
"""

import ast
import inspect
import textwrap

import pytest

import domains.learning.realtime.cascade_session as cs
from core.persona_prompt import build_system_instruction


class _Wire:
    def __init__(self) -> None:
        self.log: list = []

    async def send_event(self, event: dict) -> None:
        self.log.append(("event", event))

    async def send_audio(self, frame: bytes) -> None:
        self.log.append(("audio", len(frame)))

    async def receive(self):
        raise AssertionError("쓰지 않는다")


class _Chat:
    """LLM 스트림 대역 — **닫혔는지**를 기록한다(그게 이 기능의 핵심 동작이다)."""

    def __init__(self, pieces: list[str]) -> None:
        self._pieces = pieces
        self.text = ""
        self.usage_metadata = None
        self.failed = False
        self.truncated = False
        self.closed = False
        self.consumed = 0

    def chunks(self):
        chat = self

        async def _gen():
            try:
                for piece in chat._pieces:
                    chat.consumed += 1
                    chat.text += piece
                    yield piece
            finally:
                chat.closed = True

        return _gen()


def _rig(monkeypatch, pieces, cap=4):
    async def _stream(text, **kwargs):
        async def _gen():
            yield bytes([40]) * 480
        return _gen()

    monkeypatch.setattr(cs.tts, "synthesize_stream", _stream)
    monkeypatch.setattr(cs.settings, "CASCADE_LLM_MAX_SENTENCES", cap)
    chat = _Chat(pieces)
    monkeypatch.setattr(cs.gemini_chat, "open_chat_stream", lambda *a, **k: chat)
    session = cs.CascadeSession(_Wire(), object())
    session.beaver.lead_ms = 100_000
    return session, chat


@pytest.mark.asyncio
async def test_the_stream_is_closed_once_the_cap_is_reached(monkeypatch):
    """⭐ 4문장을 채우면 **5번째를 기다리지 않는다** — 원가·지연이 같이 준다."""
    pieces = [f"문장 {i} 입니다. " for i in range(1, 9)]
    session, chat = _rig(monkeypatch, pieces, cap=4)

    await session._run_reply("안녕")

    assert chat.consumed < len(pieces), (
        f"상한을 채우고도 끝까지 받았다({chat.consumed}/{len(pieces)} 조각)"
    )
    assert chat.consumed == 4, chat.consumed
    assert chat.closed, "스트림을 안 닫았다 — 생성이 계속되면 값을 계속 낸다"


@pytest.mark.asyncio
async def test_nothing_is_spoken_beyond_a_sentence_boundary(monkeypatch):
    """⛔ **꼬리 버림이 이 변경의 목표다.** 상한에서 끊겨도 말은 문장 경계에서 끝난다."""
    pieces = ["첫째 문장입니다. ", "둘째 문장입니다. ", "셋째 문장입니다. ",
              "넷째 문장입니다. ", "다섯째 문장의 앞부분만 왔"]
    session, chat = _rig(monkeypatch, pieces, cap=4)

    await session._run_reply("안녕")

    spoken = "".join(h["text"] for _, h in session.transport.log
                     if _ == "event" and h.get("type") == "sentence")
    assert "앞부분" not in spoken, f"미완성 문장을 말했다: {spoken!r}"
    assert spoken.count("문장") == 4, spoken


@pytest.mark.asyncio
async def test_a_short_reply_is_untouched(monkeypatch):
    """⭐ 상한보다 짧으면 **아무 일도 안 한다** — 흔한 경우를 바꾸면 안 된다."""
    pieces = ["아주 짧게 답해요. ", "그게 전부입니다."]
    session, chat = _rig(monkeypatch, pieces, cap=4)

    await session._run_reply("안녕")

    assert chat.consumed == len(pieces), "짧은 대답인데 중간에 끊었다"
    # ⚠ 마커 개수는 **문장 수가 아니라 TTS 구간 수**다(짧은 문장은 한 요청으로 묶인다 —
    #   429 대응으로 원래 그렇게 돈다). 여기서 볼 것은 **말한 내용이 온전한가**다.
    spoken = " ".join(h["text"] for _, h in session.transport.log
                      if _ == "event" and h.get("type") == "sentence")
    assert "아주 짧게 답해요." in spoken and "그게 전부입니다." in spoken, spoken


@pytest.mark.asyncio
async def test_zero_means_no_sentence_cap(monkeypatch):
    """⚠ 0 = 상한 없음. **되돌릴 길**을 남긴다(토큰 안전망만 남는다)."""
    pieces = [f"문장 {i} 입니다. " for i in range(1, 7)]
    session, chat = _rig(monkeypatch, pieces, cap=0)

    await session._run_reply("안녕")

    assert chat.consumed == len(pieces), "상한 0 인데 끊었다"


def test_the_length_rule_has_no_cascade_only_branch():
    """⛔ **전용 분기를 지웠다** — Live 와 캐스케이드가 같은 길이 규칙을 쓴다.

    분기를 되살리면 두 경로의 지시문이 다시 갈리고, 그러면 어느 쪽이 사장님이 들으신
    그것인지 아무도 답 못 한다. 죽은 코드도 남기지 않는다(사장님 규칙).
    """
    import core.persona_prompt as pp

    assert not hasattr(pp, "_RULE5_LENGTH_SHORT"), "짧은판 상수가 남아 있다"
    assert "short_reply" not in inspect.signature(build_system_instruction).parameters, (
        "옵트인 인자가 남아 있다 — 갈래가 있으면 언젠가 갈린다"
    )
    src = textwrap.dedent(inspect.getsource(cs.CascadeSession._system_instruction))
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            assert not any(kw.arg == "short_reply" for kw in node.keywords), (
                "캐스케이드가 아직 전용 길이 규칙을 켠다"
            )


def test_both_paths_get_the_same_length_rule():
    """⭐ 되돌린 결과 확인 — 원래 문구(1~4문장)가 그대로 있고, 그게 **양쪽 공통**이다."""
    text = build_system_instruction(
        role="r", personality="p", level_profile="", locale="en",
        interests=[], target_language="한국어",
    )
    assert "1~4문장으로 짧게" in text, "원본 규칙 5 로 안 돌아왔다"
    assert "1~2문장" not in text, "캐스케이드 문구가 남아 있다"
