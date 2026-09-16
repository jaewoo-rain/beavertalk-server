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
            return schema(taught=r, retaught=getattr(self, "retaught", []))     # 8차 C — 세트 이탈 표시
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
    # 8차 B: 세트가 다 확정돼 창이 이미 닫혔고, 이 턴은 «유예 판정» 1콜(세트 밖 후보만) — 확정된 세트 항목은 다시 묻지 않는다.
    assert len(fake.calls) == n_calls + 1
    assert st.expr_quiz_llm_decided == {1, 3} and 101 in st.expr_quiz_pass and 103 in st.expr_quiz_fail, "확정은 그대로(단조)"


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
    assert "· 타임아웃(6.0s) 가르침 0·정답 0" in line
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



# --------------------------------------------------------------------------- #
# 5차 B (2026-09-15, 1615 #3) — 물었고 답했는데 그 표현이 아니면 failed(무응답·딴 얘기만 pending)
# --------------------------------------------------------------------------- #
def test_verdict_instruction_marks_wrong_answers_as_failed_not_pending():
    text = seeds.expression_quiz_verdict_instruction(["3. こんばんは — 뜻: 안녕하세요(저녁)"], target="일본어", locale_label="한국어")
    # 5차 B 보강(2026-09-15 리플레이 ko «사람요» → pending): 질문 직후 발화는 틀려도 답 · pending 은 무응답·명시적 회피·안 물은 항목뿐
    assert "선생님이 그 항목을 물은 **직후의 학습자 발화는 틀려도 답이다** — 그 표현이 아니면 failed 다" in text
    # 7차 ①(2026-09-15, 1618 failed ↔ 1621 pending 흔들림): «알겠어요·잠시만요·잠깐만요·네» 는 맞장구·기다려 달라 = pending, «맞아요» 는 내용 있는 대꾸 = failed
    assert "(엉뚱한 낱말·짧은 조각·«맞아요»·«그래요» 처럼 무언가를 인정·주장하는 대꾸까지) **답으로 친다** — 회피가 아니라 틀린 답이다" in text
    assert "«맞아요»·«알겠어요»" not in text, "«알겠어요» 는 failed 예시에서 뺐다"
    assert "모른다고 말했거나(«모르겠어요»·«몰라요»·«기억이 안 나요»·«힌트 주세요»)" in text and "질문을 다시 해 달라고 했거나(«뭐라고요?»·«다시 말해 주세요»)" in text
    assert "알아들었다·기다려 달라는 반응만 했을 때(«알겠어요»·«네»·«잠시만요»·«잠깐만요»)뿐이다" in text
    assert "«맞아요» 는 이 반응이 아니라 틀린 답이다" in text, "리플레이: «맞장구» 목록이 «맞아요» 까지 pending 으로 삼켰다(7차 첫 문구)"
    assert "선생님이 아직 묻지 않은 항목도 pending." in text
    assert "다른 말만 함" not in text, "«다른 말» 을 pending 으로 읽게 하던 문구 제거"



# --------------------------------------------------------------------------- #
# 6차 A (2026-09-15, 재검 1618~1620) — 세트가 전부 확정되면 즉시 닫고 다음 큐 arm 가능
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_quiz_window_closes_as_soon_as_every_set_item_is_decided_and_the_next_cue_can_arm(monkeypatch):
    fake = FakeJudge(verdict_fn=lambda p, s: {"verdicts": [{"num": 1, "verdict": "passed"}, {"num": 2, "verdict": "failed"}, {"num": 3, "verdict": "passed"}]})
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    items = [{"item_id": 300 + i, "obj": "表現%d" % i, "des": "뜻%d" % i, "ex": None} for i in range(1, 8)]
    st = _state(items)
    st.covered_nums = [1, 2, 3, 4, 5, 6]                  # 퀴즈 중에 이미 4·5·6 을 가르쳤다
    _open_quiz(st, [1, 2, 3])
    st.covered_nums = [1, 2, 3, 4, 5, 6]
    _user(st, "表現1 表現3")
    await _drain(st)
    assert st.expr_quiz_open is False, "세트 3개가 모두 확정 → 즉시 닫힘"
    assert {301, 303} <= st.expr_quiz_pass and 302 in st.expr_quiz_fail
    assert st.expr_quiz_cue_pending is not None and st.expr_quiz_set == [4, 5, 6] and st.expr_quiz_seq == 2, "다음 큐가 바로 선다"


@pytest.mark.asyncio
async def test_quiz_window_stays_open_while_a_set_item_is_pending(monkeypatch):
    fake = FakeJudge(verdict_fn=lambda p, s: {"verdicts": [{"num": 1, "verdict": "passed"}, {"num": 2, "verdict": "passed"}, {"num": 3, "verdict": "pending"}]})
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [1, 2, 3])
    _user(st, "ありがとうございます どうも")
    await _drain(st)
    assert st.expr_quiz_open is True and st.expr_quiz_llm_decided == {1, 2}



# --------------------------------------------------------------------------- #
# 6차 B (2026-09-15, 1618) — 타임아웃 4.5초 · 타임아웃 턴은 문자열 폴백 · 계측 줄에 타임아웃 수
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_judge_timeouts_are_counted_and_fall_back_to_string_matching(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger=cs.logger.name)
    assert cs.EXPR_JUDGE_TIMEOUT_S == 6.0      # 7차(2026-09-15, 1621 최대 4506ms) 4.5 → 6.0
    monkeypatch.setattr(cs, "EXPR_JUDGE_TIMEOUT_S", 0.05)
    fake = FakeJudge(taught_fn=lambda p, s: [4], verdict_fn=lambda p, s: {"verdicts": []})
    fake.gate = asyncio.Event()                              # 영영 안 열린다 → 타임아웃
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _beaver(st, "좋아요! 'ごめんなさい' 도 따라 해 봐요. 미안할 때 쓰는 말이에요.")
    await _drain(st)
    assert 4 in st.covered_nums, "타임아웃 턴 → 문자열 폴백"
    _open_quiz(st, [1])
    _user(st, "ありがとうございます")
    await _drain(st)
    s = st.expr_judge_stats
    assert s["taught_timeout"] == 1 and s["quiz_timeout"] == 1 and s["taught_fail"] == 1 and s["quiz_fail"] == 1
    cs._log_expr_judge_summary(st, 9)
    line = [r.getMessage() for r in caplog.records if "판정 사이드카:" in r.getMessage()][-1]
    assert line.endswith("· 타임아웃(0.1s) 가르침 1·정답 1"), line
    fake.gate.set()



# --------------------------------------------------------------------------- #
# 6차 C (2026-09-15, 1620 #6 «고향» → STT «고양이» → passed) — 다른 뜻의 낱말은 통과 아님(표기 변형 통과는 유지)
# --------------------------------------------------------------------------- #
def test_verdict_instruction_rejects_a_different_word_but_keeps_spelling_variants():
    text = seeds.expression_quiz_verdict_instruction(["6. 고향 — 뜻: hometown"], target="한국어", locale_label="영어(English)")
    # 7차 ②(2026-09-15, «住みません» ↔ すみません 통과 유지): 기준 = 읽기 — 같은 읽기면 표기 무관 통과, 읽기가 다르면 소리가 비슷해도 failed
    assert "판단 기준은 글자가 아니라 **읽기(발음)** 다" in text and "같은 읽기의 다른 한자 포함" in text
    assert "표기·문자 체계가 무엇이든(가나·한자·로마자·한글 음차" in text, "표기 변형 통과는 그대로"
    assert "**읽기가 다른 낱말**이면 소리가 비슷해도 통과가 아니다 — failed 다(예: «고향» 을 물었는데 «고양이», «こんばんは» 를 물었는데 «こんにちは»)" in text
    assert "다른 뜻의 낱말" not in text
    assert text.index("받아쓰기가 조금 틀려도") < text.index("읽기가 다른 낱말"), "통과 규칙 바로 뒤의 단서"



# --------------------------------------------------------------------------- #
# 8차 A (2026-09-15, 1626 ko 번호 대응) — 통화 시작에 «번호=항목» 정본 한 줄
# --------------------------------------------------------------------------- #
def test_expression_item_list_is_logged_once_with_server_numbers(caplog):
    import logging
    caplog.set_level(logging.INFO, logger=cs.logger.name)
    cs._log_expression_items([{"item_id": 1, "obj": "ありがとうございます", "des": "감사합니다"},
                              {"item_id": 2, "obj": "どうも", "des": "고마워요", "review": True}])
    line = [r.getMessage() for r in caplog.records if "표현학습 목록:" in r.getMessage()][-1]
    assert line == "normalcall 표현학습 목록: 1=ありがとうございます · 2=どうも (2개, 복습 1)"
    caplog.clear()
    cs._log_expression_items([{"item_id": i, "obj": "표현%02d" % i} for i in range(1, 25)])
    line = [r.getMessage() for r in caplog.records if "표현학습 목록:" in r.getMessage()][-1]
    assert "1=표현01" in line and "18=표현18" in line and "19=표현19" not in line and "외 6개 (24개, 복습 0)" in line
    assert len(line) < cs.EXPR_LIST_LOG_MAX_CHARS + 120
    caplog.clear()
    cs._log_expression_items([])
    assert not [r for r in caplog.records if "표현학습 목록:" in r.getMessage()], "항목이 없으면 안 찍는다"



# --------------------------------------------------------------------------- #
# 8차 B (2026-09-15, 1624 #13) — 창이 닫힌 직후 한 턴은 «닫히기 직전 질문» 의 정답을 받는다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_correct_answer_right_after_the_window_closes_is_still_recorded(monkeypatch):
    calls = {"n": 0}

    def verdict(p, s):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"verdicts": [{"num": 1, "verdict": "passed"}, {"num": 2, "verdict": "passed"}]}
        return {"verdicts": [{"num": 4, "verdict": "passed", "why": "닫힘 직후 정답"}]}
    fake = FakeJudge(taught_fn=lambda p, s: [], verdict_fn=verdict)
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [1, 2])
    _user(st, "ありがとうございます どうも")                 # 세트 전부 확정 → 6차 A 로 즉시 닫힘
    await _drain(st)
    assert st.expr_quiz_open is False and st.expr_quiz_grace_from is not None, "유예 창이 열려 있다"
    _beaver(st, "좋아! 그럼 미안할 때는 일본어로 뭐라고 해?")   # 닫힌 뒤 비버가 세트 밖(4번)을 물었다
    await _drain(st)
    _user(st, "ごめんなさい")
    await _drain(st)
    assert 104 in st.expr_quiz_pass and 4 in st.covered_nums and 4 in st.expr_quizzed
    assert st.expr_quiz_grace_from is None, "유예는 한 번 쓰고 끝"
    n_before = calls["n"]
    _user(st, "はい")                                         # 두 번째 턴은 유예 밖
    await _drain(st)
    assert calls["n"] == n_before, "닫힘 뒤 두 번째 학습자 턴은 판정하지 않는다"


@pytest.mark.asyncio
async def test_grace_verdict_only_applies_passed_not_failed(monkeypatch):
    def verdict(p, s):
        return {"verdicts": [{"num": 1, "verdict": "passed"}]} if "[퀴즈 항목]" in s and "1. ありがとう" in s \
            else {"verdicts": [{"num": 4, "verdict": "failed", "why": "공개 뒤 복창"}]}
    fake = FakeJudge(taught_fn=lambda p, s: [], verdict_fn=verdict)
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [1])
    _user(st, "ありがとうございます")
    await _drain(st)
    assert st.expr_quiz_open is False
    _beaver(st, "미안할 때는? 'ごめんなさい' 라고 해. 따라 해 봐.")
    await _drain(st)
    covered = list(st.covered_nums)
    _user(st, "ごめんなさい")
    await _drain(st)
    assert 104 not in st.expr_quiz_pass and 104 not in st.expr_quiz_fail and st.covered_nums == covered



# --------------------------------------------------------------------------- #
# 8차 C (2026-09-15, 1624 2.5 세트 이탈) — 퀴즈 중 «이미 다룬» 항목을 다시 물으면 안내 1회(통화당 2회)
# --------------------------------------------------------------------------- #
class _Sess:
    def __init__(self):
        self.sent_text_turns: list[str] = []

    async def send_text_turn(self, text: str) -> None:
        self.sent_text_turns.append(text)


@pytest.mark.asyncio
async def test_quiz_set_drift_is_flagged_by_the_taught_judge_and_nudged_once(monkeypatch):
    fake = FakeJudge(taught_fn=lambda p, s: [], verdict_fn=lambda p, s: {"verdicts": []})
    fake.retaught = [2]
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state(JA_ITEMS + [{"item_id": 106, "obj": "またね", "des": "또 봐", "ex": None},
                            {"item_id": 107, "obj": "さようなら", "des": "안녕히 가세요", "ex": None}])
    st.covered_nums = [1, 2, 3, 4, 5]        # 6·7 이 남아 있어야 가르침 판정 사이드카가 돈다
    _open_quiz(st, [4, 5])
    st.expr_quiz_set = [4, 5]
    _beaver(st, "자, 그럼 감사합니다는 일본어로 어떻게 말했지? 다시 해 보자.")   # 세트 밖 + 이미 다룬 2번
    await _drain(st)
    assert st.expr_quiz_set_nudge_pending is True
    sess = _Sess()
    assert await cs._inject_quiz_set_reminder(sess, st) is True
    assert sess.sent_text_turns and "지금 낼 문제는 «ごめんなさい» «はい» 뿐이다" in sess.sent_text_turns[0]
    assert "이미 다룬 다른 표현은 다시 묻지 말고" in sess.sent_text_turns[0]
    assert st.expr_quiz_set_nudges == 1 and st.expr_quiz_set_nudge_pending is False
    # 세트 안 항목을 다시 물은 것은 이탈이 아니다
    cs._note_quiz_set_drift(st, [4], 9)
    assert st.expr_quiz_set_nudge_pending is False


@pytest.mark.asyncio
async def test_quiz_set_nudge_is_capped_per_call():
    st = _state()
    st.covered_nums = [1, 2, 3, 4, 5]
    _open_quiz(st, [4, 5])
    st.expr_quiz_set = [4, 5]
    sess = _Sess()
    for _ in range(2):
        st.expr_quiz_set_nudge_pending = True
        assert await cs._inject_quiz_set_reminder(sess, st) is True
    st.expr_quiz_set_nudge_pending = True
    assert await cs._inject_quiz_set_reminder(sess, st) is False, "통화당 2회 상한"
    assert len(sess.sent_text_turns) == 2 and cs.EXPR_QUIZ_SET_NUDGE_MAX == 2
    # 상한 뒤에는 표시도 서지 않는다
    st.expr_quiz_set_nudge_pending = False
    cs._note_quiz_set_drift(st, [2], 3)
    assert st.expr_quiz_set_nudge_pending is False


def test_taught_judge_instruction_asks_for_retaught_only_with_done_rows():
    base = seeds.expression_taught_judge_instruction(["1. a"], target="일본어", locale_label="한국어")
    assert "[이미 다룬 항목" not in base
    with_done = seeds.expression_taught_judge_instruction(["1. a"], target="일본어", locale_label="한국어", done_rows=["2. b"])
    assert with_done.startswith(base) and "retaught 에 적어라" in with_done and with_done.endswith("2. b")
