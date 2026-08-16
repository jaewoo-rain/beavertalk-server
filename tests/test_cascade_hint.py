"""힌트 사이드카 — **곁다리다. 메인 파이프를 1ms 도 늦추지 않는다**(2026-08-12).

사장님: "사이드카로 힌트 LLM 따로 돌리면 통화 성능 이상 없을 것 같은데" — 그 전제를 코드가
지켜야 한다. Live 의 D16 배관(지시문·스키마·프레임)을 **그대로** 쓴다(클라 변경 0).

여기서 고정하는 성질:
  ① 힌트가 늦거나 실패해도 **비버 응답 타이밍이 안 변한다** ← 이게 핵심이다
  ② `turn_id` 가 그 턴과 일치 · `korean` 빈 값 0건
  ③ 지난 턴 힌트가 **다음 턴에 안 샌다**
  ④ 힌트 LLM 예외 → **통화 계속**(R5)
  ⑤ 질문이 아닌 턴엔 안 부른다(설명 턴 힌트는 소음이고 원가만 는다)
  ⑥ **끌 수 있다**(2026-08-13) — 껐으면 만들지도, 보내지도, 호출하지도 않는다
"""

import asyncio

import pytest

import domains.learning.realtime.cascade_session as cs


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send_event(self, event: dict) -> None:
        self.events.append(event)

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def receive(self):
        raise AssertionError("쓰지 않는다")


def _session(monkeypatch) -> cs.CascadeSession:
    session = cs.CascadeSession(_Sink(), object())
    session._target_code, session._target_label = "ko", "한국어"
    return session


class _Ex:
    def __init__(self, korean, roman=None, native=""):
        self.korean, self.roman, self.native = korean, roman, native


class _Out:
    def __init__(self, examples):
        self.examples = examples


# ── ① 메인을 안 늦춘다 ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_slow_hint_never_delays_the_reply(monkeypatch):
    """⛔⛔ **이 시험이 핵심이다.** 힌트 LLM 이 아무리 느려도 대답 경로는 그만큼 안 걸린다."""
    started = asyncio.Event()

    async def _slow(*a, **kw):
        started.set()
        await asyncio.sleep(30)          # 영원히 안 끝나는 힌트
        return _Out([_Ex("안녕하세요")])

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _slow)
    session = _session(monkeypatch)

    began = asyncio.get_running_loop().time()
    session._spawn_hint("b1", "오늘 뭐 했어요?")     # 스폰은 create_task 하나뿐이다
    elapsed = asyncio.get_running_loop().time() - began
    assert elapsed < 0.05, elapsed

    await asyncio.wait_for(started.wait(), 1.0)    # 백그라운드에서만 돈다
    assert session._hint_task is not None and not session._hint_task.done()
    session._hint_task.cancel()


@pytest.mark.asyncio
async def test_a_failing_hint_keeps_the_call_alive(monkeypatch, caplog):
    """④ 힌트 LLM 이 터져도 통화는 계속된다 — 미표시로 끝난다(R5)."""
    import logging

    async def _boom(*a, **kw):
        raise RuntimeError("판정 모델 다운")

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _boom)
    session = _session(monkeypatch)
    with caplog.at_level(logging.WARNING):
        session._spawn_hint("b1", "뭐 했어요?")
        await asyncio.gather(session._hint_task, return_exceptions=True)

    assert any("힌트 사이드카 실패" in r.getMessage() for r in caplog.records), caplog.text
    assert not [e for e in session.transport.events if e.get("type") == "hint"]


# ── ② 프레임 계약 ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_hint_frame_matches_the_live_contract(monkeypatch):
    """⭐ Live 와 **같은 모델**을 쓴다 — 클라 변경 0 이 그 이유다."""
    async def _ok(*a, **kw):
        return _Out([_Ex("네, 좋아요", roman="ne, joayo", native="Yes, good"),
                     _Ex("  "),                       # ⛔ 빈 korean 은 버린다
                     _Ex("아니요")])

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _ok)
    session = _session(monkeypatch)
    await session.beaver.begin()
    session._spawn_hint("b1", "오늘 뭐 했어요?")
    await session._hint_task

    hints = [e for e in session.transport.events if e.get("type") == "hint"]
    assert len(hints) == 1, session.transport.events
    assert hints[0]["turn_id"] == "b1"
    korean = [ex["korean"] for ex in hints[0]["examples"]]
    assert korean == ["네, 좋아요", "아니요"], hints
    assert all(ex["korean"].strip() for ex in hints[0]["examples"])


# ── ③ 낡은 힌트가 안 샌다 ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_new_question_cancels_the_previous_hint(monkeypatch):
    """③ 낡은 질문의 힌트가 다음 턴에 뜨면 **학습자가 엉뚱한 예시**를 본다."""
    async def _slow(*a, **kw):
        await asyncio.sleep(5)
        return _Out([_Ex("늦은 힌트")])

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _slow)
    session = _session(monkeypatch)
    session._spawn_hint("b1", "첫 질문이에요?")
    first = session._hint_task
    session._spawn_hint("b2", "둘째 질문이에요?")

    await asyncio.sleep(0)
    assert first.cancelled() or first.done(), "앞 힌트가 안 취소됐다"
    session._hint_task.cancel()


@pytest.mark.asyncio
async def test_a_late_hint_for_an_old_turn_is_dropped(monkeypatch):
    """취소를 못 맞았더라도 **턴이 지났으면 안 보낸다**."""
    async def _ok(*a, **kw):
        return _Out([_Ex("예시")])

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _ok)
    session = _session(monkeypatch)
    await session.beaver.begin()          # 지금 턴은 b1
    session._hint_task = object()         # '나는 최신 태스크가 아니다'를 만든다
    await session._run_hint({"client": object(), "model": "m", "instruction": "i"},
                            "b0", "지난 질문이에요?")
    assert not [e for e in session.transport.events if e.get("type") == "hint"]


# ── ⑤ 질문일 때만 ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_non_question_turn_does_not_call_the_model(monkeypatch):
    """⑤ 설명 턴에 힌트를 띄우면 소음이고, 매 턴 부르면 원가만 는다(Live 와 같은 규칙)."""
    called = {"n": 0}

    async def _count(*a, **kw):
        called["n"] += 1
        return None

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _count)
    session = _session(monkeypatch)
    session._spawn_hint("b1", "오늘은 인사를 배웠어요.")
    assert session._hint_task is None and called["n"] == 0


def test_a_conversation_only_language_gets_no_hints(monkeypatch):
    """⚠ 커리큘럼 없는 언어는 예시 생성 프롬프트가 안 맞는다 — Live 와 같은 조건(게이트만 시험).

    ⛔ 레지스트리 값으로 시험하지 않는다: 지금 `SUPPORTED_LANGUAGES` 는 **전 언어가
      `has_curriculum=True`** 인데 바로 위 주석은 "en/zh/fr/vi 는 아직 회화 전용"이라고
      말한다 — **주석이 낡았다.** 그 불일치에 시험을 매달면 이 시험이 무엇을 지키는지 흐려진다.
      여기서 지키는 건 "커리큘럼이 없으면 힌트를 안 띄운다"는 **우리 게이트**다.
    """
    from core.languages import LanguageSpec

    session = cs.CascadeSession(_Sink(), object())
    monkeypatch.setattr(
        cs, "resolve_language",
        lambda code: LanguageSpec(code, "회화전용", 13, False, False),
    )
    session._spawn_hint("b1", "뭐 했어요?")
    assert session._hint_task is None


def test_without_an_llm_client_nothing_is_spawned():
    """R5 — 키가 없으면 힌트만 없다(통화는 돈다)."""
    session = cs.CascadeSession(_Sink(), None)
    session._spawn_hint("b1", "뭐 했어요?")
    assert session._hint_task is None


# ── ⑥ 순정 모드: 끌 수 있다 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_switch_stops_the_sidecar_entirely(monkeypatch):
    """⭐⭐ 사장님 방향(2026-08-13): "기능은 하나씩 추가하자" — 먼저 **순정으로 벗겨** 돌린다.

    ⛔ 프론트가 화면에서 안 그리는 것으로는 부족하다. **서버가 안 만들어야** 턴당 LLM 호출이
      1건 줄고 로그가 조용해진다 — 순정 판정에 잡음이 없어야 한다.
    """
    called = {"n": 0}

    async def _count(*a, **kw):
        called["n"] += 1
        return None

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _count)
    monkeypatch.setattr(cs.settings, "CASCADE_HINT_ENABLED", False)
    session = _session(monkeypatch)

    session._spawn_hint("b1", "주말에 뭐 했어요?")      # 원래라면 힌트가 나가는 조건이다
    await asyncio.sleep(0.05)

    assert session._hint_task is None, "껐는데 태스크를 띄웠다"
    assert called["n"] == 0, "껐는데 LLM 을 불렀다 — 원가·로그가 그대로 는다"
    assert not [e for e in session.transport.events if e.get("type") == "hint"], (
        session.transport.events
    )


@pytest.mark.asyncio
async def test_the_call_works_the_same_with_hints_off(monkeypatch):
    """⚠ **꺼도 통화는 그대로 돈다** — 힌트는 원래 곁다리다.

    끄는 스위치가 대답 경로를 건드리면 순정 판정 자체가 오염된다(무엇을 재는지 모르게 된다).
    """
    async def _stream(text, **kwargs):
        async def _gen():
            yield bytes([40]) * 480
        return _gen()

    monkeypatch.setattr(cs.tts, "synthesize_stream", _stream)
    monkeypatch.setattr(cs.settings, "CASCADE_HINT_ENABLED", False)
    session = _session(monkeypatch)
    session.beaver.lead_ms = 100_000

    await session.beaver.begin()
    sent = await session._speak("<happy> 주말에 뭐 했어요?")

    assert sent > 0, "힌트를 껐더니 말을 안 한다"
    assert [e["type"] for e in session.transport.events if e.get("type") == "sentence"] == [
        "sentence"
    ], session.transport.events


def test_the_switch_defaults_to_on():
    """⚠ 기본은 켜짐이다 — 스위치를 넣었다고 기존 동작이 조용히 바뀌면 안 된다."""
    from core.config import Settings

    assert Settings.model_fields["CASCADE_HINT_ENABLED"].default is True
