"""대답 길이 상한 — **기능적 상한**만 건다(프롬프트 문구 튜닝은 범위 밖).

왜(2026-08-12 실통화 00146): 한 대답이 **18.7초(196자)** 였고, 그동안 사장님 발화가
대기열에서 **14.2초**를 기다렸다. 사장님: "끼어들어도 이전 이야기 끝내고 다음 이야기하는
것 같다." 어학 대화에서 선생님이 19초를 혼자 말하는 것 자체가 제품 문제다.

값(40토큰)의 근거 — 같은 모델·같은 대본으로 **꼬리를 버린 뒤 실제 발화 길이**를 실측했다:
    상한 없음 15.4초(최악 19.2) · 36 → 8.2초 · **40 → 9.1초** · 44 → 11.5초 · 64 → 12.6초
⚠ **너무 짧아도 안 된다**(사장님) — 설명·예시·질문이 한 턴에 들어가야 한다. 40 에서도
  8회 중 1회는 문장 1개(5.0초)로 얇아졌다. 그래서 env 로 올릴 수 있게 뒀다.

여기서 고정하는 성질:
  ① 상한에 걸려도 **문장 중간에서 끊긴 말은 내보내지 않는다**
  ② 잘렸다는 사실이 **로그에 남는다**(조용히 잘리면 원인을 못 찾는다)
  ③ 상한은 **env 로 바뀐다**(사장님이 들어보고 조절하실 값이다)
  ④ 버린 꼬리는 **이력에도 안 들어간다**(하지도 않은 말을 했다고 믿으면 안 된다)
"""

import inspect

import pytest

import core.gemini_chat as gemini_chat
import domains.learning.realtime.cascade_session as cs


class _Sink:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        self.frames.append(frame)

    async def receive(self):
        raise AssertionError("쓰지 않는다")


# ── ③ env 로 바뀐다 + 호출 파라미터로 나간다 ───────────────────────────────
def test_the_cap_is_a_call_parameter_not_a_prompt_sentence():
    """⛔ 프롬프트 문구가 아니라 **벤더 파라미터**다(이번 범위의 핵심 제약).

    문구 튜닝은 사장님이 범위 밖으로 그으셨다 — 페르소나를 건드리지 않는다.
    """
    src = inspect.getsource(gemini_chat.open_chat_stream)
    assert "max_output_tokens" in src
    assert "max_output_tokens" in inspect.getsource(cs.CascadeSession._run_reply)


def test_the_cap_is_configurable_and_sane_by_default():
    from core.config import settings

    assert settings.CASCADE_LLM_MAX_OUTPUT_TOKENS > 0, "기본값이 상한 없음이면 의미가 없다"
    # 실측 표 기준: 36~64 밖으로 나가면 '너무 짧다/의미 없다' 중 하나다.
    assert 30 <= settings.CASCADE_LLM_MAX_OUTPUT_TOKENS <= 64


def test_zero_means_no_cap(monkeypatch):
    """0 = 벤더 기본값(상한 없음) — 되돌릴 길을 남긴다."""
    captured: dict = {}

    class _Types:
        class ThinkingConfig:
            def __init__(self, **kw):
                pass

        class Content:
            def __init__(self, **kw):
                pass

        class Part:
            def __init__(self, **kw):
                pass

        class GenerateContentConfig:
            def __init__(self, **kw):
                captured.update(kw)

    monkeypatch.setitem(__import__("sys").modules, "google.genai", type(
        "M", (), {"types": _Types})())
    stream = gemini_chat.open_chat_stream(
        object(), "m", system_instruction="s", history=[], user_text="u",
        max_output_tokens=0,
    )
    assert stream is not None
    assert "max_output_tokens" not in captured

    captured.clear()
    gemini_chat.open_chat_stream(
        object(), "m", system_instruction="s", history=[], user_text="u",
        max_output_tokens=40,
    )
    assert captured["max_output_tokens"] == 40


# ── ①④ 잘린 꼬리는 말하지도, 기억하지도 않는다 ────────────────────────────
def test_the_incomplete_tail_is_dropped():
    """⛔ "…그리고 저는" 하고 끝나면 상한을 안 두느니만 못하다."""
    session = cs.CascadeSession(_Sink())
    kept, dropped = session._drop_incomplete_tail(
        "안녕하세요. 오늘은 인사를 배워요. 그리고 저는"
    )
    assert kept == "안녕하세요. 오늘은 인사를 배워요."
    assert dropped == "그리고 저는"


def test_nothing_is_dropped_when_no_sentence_is_complete(caplog):
    """⚠ 완성된 문장이 하나도 없으면 **버리지 않는다** — 침묵보다 미완성이 낫다.

    ⛔ 대신 그 사실을 크게 남긴다(상한이 첫 문장도 못 담았다는 신호다).
    """
    import logging

    session = cs.CascadeSession(_Sink())
    with caplog.at_level(logging.WARNING):
        kept, dropped = session._drop_incomplete_tail("안녕하세요 저는 비버라고")
    assert kept == "안녕하세요 저는 비버라고" and dropped == ""
    assert any("완성된 문장이 하나도 없다" in r.getMessage() for r in caplog.records)


def test_a_complete_reply_is_untouched():
    """⛔ 안 잘린 대답은 **한 글자도 안 건드린다**(상한이 멀쩡한 말을 깎으면 안 된다)."""
    session = cs.CascadeSession(_Sink())
    kept, dropped = session._drop_incomplete_tail("안녕하세요. 반가워요!")
    assert kept == "안녕하세요. 반가워요!" and dropped == ""


# ── ①②④ 실제 대답 경로 ────────────────────────────────────────────────────
class _FakeChat:
    """ChatStream 대역 — 상한에 걸린 응답을 흉내 낸다."""

    def __init__(self, pieces, truncated: bool) -> None:
        self._pieces = pieces
        self.text = ""
        self.usage_metadata = None
        self.failed = False
        self.truncated = truncated

    async def chunks(self):
        for piece in self._pieces:
            self.text += piece
            yield piece


async def _run(monkeypatch, pieces, truncated: bool):
    spoken: list[str] = []

    async def _speak(self, text):
        spoken.append(text)
        return len(text) * 100

    monkeypatch.setattr(cs.CascadeSession, "_speak", _speak)
    monkeypatch.setattr(cs.CascadeSession, "_begin_beaver_turn",
                        lambda self: _turn(self))
    session = cs.CascadeSession(_Sink())
    chat = _FakeChat(pieces, truncated)
    monkeypatch.setattr(cs.gemini_chat, "open_chat_stream", lambda *a, **kw: chat)
    session._genai_client = object()
    await session._run_reply("안녕", 0)
    return session, spoken


async def _turn(session):
    return await session.beaver.begin()


@pytest.mark.asyncio
async def test_a_truncated_reply_never_speaks_the_broken_sentence(monkeypatch, caplog):
    """①② 잘린 꼬리는 **소리로 안 나가고**, 잘렸다는 사실은 **로그에 남는다**."""
    import logging

    with caplog.at_level(logging.INFO):
        session, spoken = await _run(
            monkeypatch, ["안녕하세요. 오늘은 인사를 배워요. ", "그리고 저는"], True
        )
    said = " ".join(spoken)
    assert "그리고 저는" not in said, said
    assert "인사를 배워요" in said
    lines = [r.getMessage() for r in caplog.records if "cascade 대답" in r.getMessage()]
    assert lines and "상한잘림" in lines[-1], lines


@pytest.mark.asyncio
async def test_the_dropped_tail_never_enters_the_history(monkeypatch):
    """④ 하지도 않은 말을 이력에 넣으면 다음 턴의 모델이 그걸 했다고 믿는다."""
    session, _ = await _run(
        monkeypatch, ["안녕하세요. 오늘은 인사를 배워요. ", "그리고 저는"], True
    )
    model_turns = [h["text"] for h in session._history if h["role"] == "model"]
    assert model_turns, session._history
    assert "그리고 저는" not in model_turns[-1], model_turns[-1]


@pytest.mark.asyncio
async def test_an_unclipped_reply_keeps_its_last_sentence(monkeypatch):
    """⛔ 안 잘린 대답의 마지막 조각은 **그대로 나간다**(꼬리 버리기가 오작동하면 안 된다)."""
    session, spoken = await _run(monkeypatch, ["안녕하세요. ", "반가워요"], False)
    assert "반가워요" in " ".join(spoken)
