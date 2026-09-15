"""4차(2026-09-15) 표현학습 판정을 LLM 사이드카로 — 의미 판단 = LLM · 순서·개수·기록 = 서버. 가짜 generate_structured 로 본다.

A «가르쳤나»: 비버 턴마다 사이드카 1회 → 번호를 covered_nums 에 append → 종전 퀴즈 상태기계. 실패 턴은 문자열 폴백. 짧은 리액션은 건너뜀.
"""
from __future__ import annotations

import asyncio

import pytest

import domains.learning.realtime.call_session as cs
from core.prompts.locked import seeds

JA_ITEMS = [
    {"item_id": 101, "obj": "ありがとうございます", "des": "감사합니다(정중하게)", "ex": None},
    {"item_id": 102, "obj": "どうも", "des": "고마워요(가볍게·아주 짧게)", "ex": None},
    {"item_id": 103, "obj": "すみません", "des": "죄송합니다·저기요", "ex": None},
    {"item_id": 104, "obj": "ごめんなさい", "des": "미안해요", "ex": None},
    {"item_id": 105, "obj": "はい", "des": "네", "ex": None},
]


def _state(items=JA_ITEMS, lang="ja") -> cs._CallState:
    st = cs._CallState()
    st.expr_items = list(items)
    st.reground_items = [i["obj"] for i in items]
    st.target_code = lang
    st.expr_ctx = {"client": object(), "model": "m", "locale_label": "한국어", "target_language": "일본어"}
    st.reground_persona = ("선생님", "다정함")
    st.expr_llm_judge = True
    return st


class FakeJudge:
    """schema 이름으로 갈라 답한다. taught_fn(prompt, system) → list[int] | Exception. gate 가 있으면 열릴 때까지 기다린다."""

    def __init__(self, taught_fn=None, verdict_fn=None):
        self.taught_fn = taught_fn or (lambda p, s: [])
        self.verdict_fn = verdict_fn
        self.calls: list[tuple[str, str]] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, client, model, *, system_instruction, prompt, schema, **_kw):
        self.calls.append((schema.__name__, prompt))
        if self.gate is not None:
            await self.gate.wait()
        if schema.__name__ == "ExpressionTaughtOut":
            r = self.taught_fn(prompt, system_instruction)
            if isinstance(r, Exception):
                raise r
            return schema(taught=r)
        r = self.verdict_fn(prompt, system_instruction)
        if isinstance(r, Exception):
            raise r
        return schema(**r)


async def _drain(st):
    for _ in range(50):
        pending = [t for t in st.expr_tasks if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def _beaver(st, text):
    st.cur_beaver_text = [text]
    cs._flush_beaver_segment(st)


def _user(st, text):
    st.cur_user_text = [text]
    cs._flush_user_segment(st)


@pytest.mark.asyncio
async def test_taught_is_judged_by_meaning_even_without_the_target_surface(monkeypatch):
    """1614: 비버가 한국어로 «'고마워요'는 일본어로?» 만 물은 턴 — 표면형이 없어 문자열로는 «안 가르침». LLM 이 2번으로 판정한다."""
    fake = FakeJudge(taught_fn=lambda p, s: [2] if "고마워요" in p else [])
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _beaver(st, "좋아, 다음! 친구한테 가볍게 '고마워요' 할 때는 일본어로 뭐라고 할까?")
    assert st.covered_nums == [], "문자열 대조를 쓰지 않는다(표면형 없음) — 판정은 비동기"
    await _drain(st)
    assert st.covered_nums == [2]
    name, prompt = fake.calls[0]
    assert name == "ExpressionTaughtOut" and "[선생님 이번 턴]" in prompt and "B: 좋아, 다음!" in prompt
    assert st.expr_judge_stats["taught_calls"] == 1 and st.expr_judge_stats["lat_ms"]


@pytest.mark.asyncio
async def test_taught_judge_failure_falls_back_to_string_matching_for_that_turn(monkeypatch):
    fake = FakeJudge(taught_fn=lambda p, s: RuntimeError("boom"))
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _user(st, "すみません")                                   # 직전 학습자 턴 — 폴백에서만 산다
    _beaver(st, "좋아요! 'ごめんなさい' 도 따라 해 봐요. 미안할 때 쓰는 말이에요.")
    assert st.covered_nums == [], "학습자 발화는 LLM 판정 중엔 가르침을 세지 않는다"
    await _drain(st)
    assert sorted(st.covered_nums) == [3, 4], "사이드카 실패 → 비버 턴 문자열(4) + 직전 학습자 턴 문자열(3)"
    assert st.expr_judge_stats["taught_fail"] == 1


@pytest.mark.asyncio
async def test_short_reaction_turn_without_candidates_is_not_sent(monkeypatch):
    fake = FakeJudge(taught_fn=lambda p, s: [1])
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _beaver(st, "오, 좋아!")                                  # 20자 미만 · 후보 없음
    await _drain(st)
    assert fake.calls == [] and st.covered_nums == [] and st.expr_judge_stats["taught_skip"] == 1
    _beaver(st, "좋아, 미안해요!")                             # 짧아도 남은 항목의 뜻 조각(«미안해요»)이 있으면 보낸다
    await _drain(st)
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_invalid_or_already_covered_numbers_are_ignored(monkeypatch):
    fake = FakeJudge(taught_fn=lambda p, s: [1, 1, 9, 0, 3])
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    st.covered_nums = [3]
    _beaver(st, "이번에는 감사합니다를 정중하게 말하는 표현을 배워 볼 거예요. 일본어로 뭐라고 할까요?")
    await _drain(st)
    assert st.covered_nums == [3, 1], "범위 밖·중복·이미 covered 는 버린다(append-only)"


@pytest.mark.asyncio
async def test_a_late_taught_result_closes_the_quiz_at_that_beaver_turn_not_later(monkeypatch):
    """늦게 온 가르침 판정이 닫힘 트리거면 창 끝은 **그 비버 턴** — 그 뒤에 온 학습자 답은 창 밖(미판정)."""
    items = JA_ITEMS
    fake = FakeJudge(taught_fn=lambda p, s: [4] if "미안해요" in p else [])
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state(items)
    st.covered_nums = [1, 2, 3]
    st.expr_quizzed = {1, 2, 3}
    st.expr_quiz_set = [1, 2, 3]
    st.expr_quiz_seq = 1
    st.expr_quiz_awaiting_open = True
    cs._expression_quiz_open_on_beaver_turn(st)
    open_seg = st.expr_quiz_open_seg
    _beaver(st, "퀴즈! 감사합니다를 정중하게는?")          # open_seg
    await _drain(st)
    _user(st, "ありがとうございます")
    fake.gate = asyncio.Event()                             # 다음 판정은 늦게 온다
    _beaver(st, "좋아! 이제 새 표현, 미안할 때 쓰는 말 '미안해요' 는 일본어로?")   # 닫힘 트리거 턴(4번 소개)
    k = len(st.segments) - 1
    _user(st, "どうも")                                     # 트리거 뒤 — 창 밖이어야 한다
    fake.gate.set()
    await _drain(st)
    assert st.expr_quiz_open is False
    assert 101 in st.expr_quiz_pass, "창 안 답"
    assert 102 not in st.expr_quiz_pass, "트리거 턴 뒤 답은 창 밖"
    assert k == open_seg + 2


@pytest.mark.asyncio
async def test_learner_utterances_do_not_count_as_taught_while_llm_judging(monkeypatch):
    fake = FakeJudge(taught_fn=lambda p, s: [])
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _user(st, "はい")
    await _drain(st)
    assert st.covered_nums == []
    st.expr_llm_judge = False                               # 스위치 꺼짐 → 종전(학습자 발화도 covered)
    _user(st, "はい")
    assert st.covered_nums == [5]


def test_taught_judge_instruction_pins_the_rules():
    text = seeds.expression_taught_judge_instruction(["1. どうも — 뜻: 고마워요"], target="일본어", locale_label="한국어")
    assert "이번 B 턴에서" in text and "한글 음차" in text and "학습자만 말하고 선생님이 이 턴에서 다루지 않은 항목은 넣지 마라" in text
    assert "1. どうも — 뜻: 고마워요" in text
