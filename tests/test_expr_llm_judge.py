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



# --------------------------------------------------------------------------- #
# B «맞혔나» — 퀴즈 창 안 학습자 턴마다 항목별 passed/failed/pending · 닫힐 때 남은 것만 한 번 더 · 실패면 서버 문자열
# --------------------------------------------------------------------------- #
def _open_quiz(st, nums, seq=1):
    st.covered_nums = sorted(set(st.covered_nums) | set(nums))
    st.expr_quizzed = set(nums)
    st.expr_quiz_set = list(nums)
    st.expr_quiz_seq = seq
    st.expr_quiz_awaiting_open = True
    cs._expression_quiz_open_on_beaver_turn(st)


@pytest.mark.asyncio
async def test_learner_answer_in_hangul_transliteration_is_passed_by_the_llm(monkeypatch):
    """1614 류: 「どうも」 를 «도모» 로 말했다 — 문자열로는 틀림, LLM 판정은 통과."""
    fake = FakeJudge(verdict_fn=lambda p, s: {"verdicts": [{"num": 2, "verdict": "passed", "why": "음차"}]} if "U" in p else {"verdicts": []})
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [2])
    _beaver(st, "퀴즈! 친구한테 가볍게 고마워요는?")
    await _drain(st)
    _user(st, "도모")
    await _drain(st)
    assert 102 in st.expr_quiz_pass and st.expr_quiz_llm_decided == {2}
    name, prompt = [c for c in fake.calls if c[0] == "ExpressionVerdictOut"][0]
    assert "[퀴즈 전사]" in prompt and "U" in prompt and "도모" in prompt


@pytest.mark.asyncio
async def test_repeat_after_reveal_is_failed_and_failed_cannot_undo_passed(monkeypatch):
    answers = iter([{"verdicts": [{"num": 3, "verdict": "failed", "why": "공개 뒤 복창"}, {"num": 1, "verdict": "passed"}]},
                    {"verdicts": [{"num": 1, "verdict": "failed"}]}])
    fake = FakeJudge(verdict_fn=lambda p, s: next(answers))
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [1, 3])
    _user(st, "すみません")
    await _drain(st)
    assert 103 in st.expr_quiz_fail and 101 in st.expr_quiz_pass
    assert st.expr_quiz_llm_decided == {1, 3}
    # 확정된 항목은 다음 턴 판정에 다시 안 보낸다
    n_calls = len(fake.calls)
    _user(st, "ありがとう")
    await _drain(st)
    assert len(fake.calls) == n_calls, "확정 뒤엔 부를 게 없다"


@pytest.mark.asyncio
async def test_pending_items_get_one_more_llm_call_at_close_and_fall_back_to_server_matching_on_failure(monkeypatch):
    calls = {"n": 0}

    def verdict(p, s):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"verdicts": [{"num": 5, "verdict": "pending"}]}
        return RuntimeError("boom")                       # 닫힐 때 1콜 → 실패 → 서버 문자열
    fake = FakeJudge(verdict_fn=verdict)
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [5])
    _user(st, "はい です")
    await _drain(st)
    assert 105 not in st.expr_quiz_pass and st.expr_quiz_llm_decided == set()
    cs._close_expression_quiz(st, why="시험")
    await _drain(st)
    assert calls["n"] == 2, "닫힐 때 남은 항목으로 한 번 더"
    assert 105 in st.expr_quiz_pass, "LLM 실패 → 서버 문자열 판정(はい 표면형이 창 안 U 에 있다)"
    assert st.expr_judge_stats["quiz_fail"] == 1


@pytest.mark.asyncio
async def test_a_late_verdict_does_not_pollute_the_next_quiz(monkeypatch):
    fake = FakeJudge(verdict_fn=lambda p, s: {"verdicts": [{"num": 1, "verdict": "passed"}]})
    fake.gate = asyncio.Event()
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [1], seq=1)
    _user(st, "ありがとうございます")                        # 판정이 늦게 온다
    st.expr_quiz_open = False                               # 그 사이 퀴즈 1 이 닫히고 퀴즈 2 가 열렸다
    _open_quiz(st, [1, 2], seq=2)                           # (재출제 가정 — 같은 번호 1 이 새 퀴즈에도 있다)
    fake.gate.set()
    await _drain(st)
    assert 101 in st.expr_quiz_pass, "통과 사실(item_id)은 반영"
    assert st.expr_quiz_llm_decided == set(), "퀴즈 2 의 확정 집합은 오염되지 않는다"


@pytest.mark.asyncio
async def test_final_progress_judges_the_open_quiz_with_the_llm_within_budget(monkeypatch):
    fake = FakeJudge(verdict_fn=lambda p, s: {"verdicts": [{"num": 4, "verdict": "passed"}]})
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    st.expr_llm_judge = True
    _open_quiz(st, [4])
    st.cur_user_text = ["고멘나사이"]                       # 아직 flush 안 된 꼬리 — 마지막 판정이 본다
    fake.gate = asyncio.Event()
    fake.gate.set()
    await cs._final_expression_progress(st)
    assert st.expr_quiz_open is False and 104 in st.expr_quiz_pass


def test_quiz_verdict_instruction_pins_the_four_rules():
    text = seeds.expression_quiz_verdict_instruction(["2. どうも — 뜻: 고마워요"], target="일본어", locale_label="한국어")
    assert "한글 음차" in text and "선생님이 그 표현(정답)을 먼저 들려준 뒤에야 학습자가 따라 말했거나" in text
    assert "반말(보통형)만 말했으면 passed 가 아니다" in text and "pending: 학습자가 그 항목에 아직 답하지 않았다" in text



# --------------------------------------------------------------------------- #
# C — 큐·재접지 쪽지에 «이미 다룬» · «남은(번호·뜻)» 목록을 서버가 싣는다(규칙 ①)
# --------------------------------------------------------------------------- #
def test_quiz_cue_carries_done_and_remaining_lists_from_the_server():
    st = _state()
    st.covered_nums = [1, 2, 3]
    cue = cs._expression_quiz_cue(st, [1, 2, 3])
    assert "이미 다룬 표현: ありがとうございます / どうも / すみません — 다시 가르치지 마라." in cue
    assert "퀴즈 뒤 새로 가르칠 남은 표현(번호·뜻): 4. 미안해요 = ごめんなさい · 5. 네 = はい — 이 순서로, 이것만 새로 가르쳐라." in cue
    assert " 지금 퀴즈를 내라" in cue


def test_reground_note_lists_what_is_left_so_the_beaver_does_not_reteach():
    st = _state()
    st.covered_nums = [1, 2]
    note = cs._build_expression_note(st)
    assert "이미 다룬 표현: ありがとうございます / どうも" in note
    assert "아직 안 가르친 남은 표현(번호·뜻): 3. 죄송합니다·저기요 = すみません · 4. 미안해요 = ごめんなさい · 5. 네 = はい." in note
    assert "이미 다룬 표현을 처음처럼 다시 가르치지 마라" in note
    st.covered_nums = [1, 2, 3, 4, 5]
    assert "아직 안 가르친 남은 표현" not in cs._build_expression_note(st), "다 가르쳤으면 줄이 없다"


def test_remaining_rows_are_capped():
    items = [{"item_id": 200 + i, "obj": "표현%d" % i, "des": "뜻%d(설명)" % i, "ex": None} for i in range(1, 16)]
    st = _state(items, lang="ko")
    rows = cs._expr_remaining_rows(st)
    assert len(rows) == cs.EXPR_REMAINING_ROWS_CAP + 1 and rows[0] == "1. 뜻1 = 표현1" and rows[-1] == "외 3개"



# --------------------------------------------------------------------------- #
# E — 판정 사이드카 계측: 호출 수·지연·토큰 한 줄 + 원가 sidecars 칸 합산
# --------------------------------------------------------------------------- #
class CountingJudge(FakeJudge):
    async def __call__(self, client, model, *, usage=None, **kw):
        if usage is not None:
            usage.calls += 1
            usage.in_text += 100
            usage.out_text += 7
            usage.thoughts += 1
        return await super().__call__(client, model, **kw)


@pytest.mark.asyncio
async def test_judge_sidecar_summary_line_and_usage_are_accounted(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger=cs.logger.name)
    fake = CountingJudge(taught_fn=lambda p, s: [2] if "고마워요" in p else [],
                         verdict_fn=lambda p, s: {"verdicts": [{"num": 2, "verdict": "passed"}]})
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _beaver(st, "오, 좋아!")                                                  # 건너뜀
    _beaver(st, "친구한테 가볍게 '고마워요' 할 때는 일본어로 뭐라고 할까?")   # 가르침 1
    await _drain(st)
    _open_quiz(st, [2])
    _user(st, "도모")                                                         # 정답 1
    await _drain(st)
    cs._log_expr_judge_summary(st, 77)
    line = [r.getMessage() for r in caplog.records if "판정 사이드카:" in r.getMessage()][-1]
    assert "call_id=77 LLM · 가르침 1회(건너뜀 1·실패 0·폴백 0) · 정답 1회(실패 0)" in line and "토큰 in 200 out 16" in line, line
    assert st.sidecar_usage.calls == 2 and st.sidecar_usage.in_text == 200, "원가 계기판 sidecars 칸으로 합산"


def test_judge_summary_is_silent_for_non_expression_calls(caplog):
    import logging
    caplog.set_level(logging.INFO, logger=cs.logger.name)
    cs._log_expr_judge_summary(cs._CallState(), 1)
    assert not [r for r in caplog.records if "판정 사이드카:" in r.getMessage()]



# --------------------------------------------------------------------------- #
# 5차 C (2026-09-15, 1617) — 상한 안에 끝난 마지막 판정을 «⚠초과» 로 찍지 않는다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_final_judge_log_is_not_marked_over_when_tasks_finish_within_budget(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger=cs.logger.name)
    monkeypatch.setattr(cs, "EXPR_FINAL_JUDGE_TIMEOUT_S", 0.6)
    st = _state()

    async def _slow():
        await asyncio.sleep(0.4)                    # 절반(0.3s) 뒤·상한(0.6s) 전에 끝난다
    task = asyncio.create_task(_slow())
    st.expr_tasks.add(task)
    await cs._final_expression_progress(st)
    line = [r.getMessage() for r in caplog.records if "마지막 판정:" in r.getMessage()][-1]
    assert "초과" not in line, line
    assert task.done()

    st2 = _state()

    async def _slower():
        await asyncio.sleep(2.0)
    t2 = asyncio.create_task(_slower())
    st2.expr_tasks.add(t2)
    await cs._final_expression_progress(st2)
    line2 = [r.getMessage() for r in caplog.records if "마지막 판정:" in r.getMessage()][-1]
    assert "⚠초과" in line2, "정말 예산을 넘기면 여전히 찍힌다"
    t2.cancel()



# --------------------------------------------------------------------------- #
# 5차 A-1 (2026-09-15, 1615·1616) — 큐 구속력: 이 N개만, 적힌 순서대로
# --------------------------------------------------------------------------- #
def test_quiz_cue_binds_the_beaver_to_the_set_in_order():
    st = _state()
    st.covered_nums = [1, 2, 3]
    cue = cs._expression_quiz_cue(st, [1, 2, 3])
    assert "이 3개만, 적힌 순서대로 물어라 — 다른 표현은 지금 묻지 마라(3개를 다 물은 뒤에 다음 새 표현으로 넘어간다)." in cue
    assert "3개가 끝나면 다음 새 표현으로 넘어가라" not in cue



# --------------------------------------------------------------------------- #
# 5차 A-2 (2026-09-15, 1615 #13·1616 #7) — 세트 밖 자발 정답 기록 · 공개 후 복창은 기록 0
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_off_set_spontaneous_answer_is_recorded_as_passed_and_covered(monkeypatch):
    def verdict(p, s):
        assert "[세트 밖 항목" in s and "4. ごめんなさい" in s, "세트 밖 후보가 지시문에 실린다"
        return {"verdicts": [{"num": 1, "verdict": "pending"}, {"num": 4, "verdict": "passed", "why": "자발 정답"}]}
    fake = FakeJudge(verdict_fn=verdict)
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [1, 2, 3])
    _beaver(st, "미안할 때는 뭐라고 해?")
    await _drain(st)
    _user(st, "ごめんなさい")
    await _drain(st)
    assert 104 in st.expr_quiz_pass and 4 in st.covered_nums and 4 in st.expr_quizzed
    assert st.expr_quiz_open is True and 4 not in st.expr_quiz_llm_decided, "세트 확정 집합·창은 그대로"


@pytest.mark.asyncio
async def test_off_set_repeat_after_reveal_records_nothing(monkeypatch):
    fake = FakeJudge(verdict_fn=lambda p, s: {"verdicts": [{"num": 4, "verdict": "failed", "why": "공개 뒤 복창"}]})
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [1, 2, 3])
    covered = list(st.covered_nums)
    _user(st, "ごめんなさい")
    await _drain(st)
    assert 104 not in st.expr_quiz_pass and 104 not in st.expr_quiz_fail, "세트 밖은 passed 만 적용 — 오답도 안 남긴다"
    assert st.covered_nums == covered and 4 not in st.expr_quizzed


def test_verdict_instruction_off_set_block_only_when_given():
    base = seeds.expression_quiz_verdict_instruction(["1. a"], target="일본어", locale_label="한국어")
    assert "[세트 밖 항목" not in base
    with_extra = seeds.expression_quiz_verdict_instruction(["1. a"], target="일본어", locale_label="한국어", extra_rows=["4. b"])
    assert with_extra.startswith(base) and with_extra.endswith("4. b") and "실제로 물은" in with_extra
