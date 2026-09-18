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
    assert s["taught_fallback"] == 1, "9차 C — 타임아웃 뒤 문자열 폴백을 탔으면 폴백 수도 올라간다(1632 계측 정합)"
    cs._log_expr_judge_summary(st, 9)
    line = [r.getMessage() for r in caplog.records if "판정 사이드카:" in r.getMessage()][-1]
    assert line.endswith("· 타임아웃(0.1s) 가르침 1·정답 1"), line
    fake.gate.set()



# --------------------------------------------------------------------------- #
# 6차 C (2026-09-15, 1620 #6 «고향» → STT «고양이» → passed) — 다른 뜻의 낱말은 통과 아님(표기 변형 통과는 유지)
# --------------------------------------------------------------------------- #
def test_verdict_instruction_rejects_a_different_word_but_keeps_spelling_variants():
    text = seeds.expression_quiz_verdict_instruction(["6. 고향 — 뜻: hometown"], target="한국어", locale_label="영어(English)")
    # 12차 B(2026-09-18, 1645 #15 — 「本当?」 만 말했는데 passed): 읽기는 **항목 전체**의 읽기여야 하고, 정중형 규칙이 읽기 규칙보다 우선한다
    assert "읽기는 **항목 전체**의 읽기여야 한다" in text and "«本当ですか» 를 물었는데 «本当?»" in text
    assert "**이 규칙이 위의 «읽기가 같으면 통과» 보다 우선한다.**" in text
    assert text.index("항목 전체**의 읽기") < text.index("정중형을 가르치는 항목"), "읽기 규칙 바로 뒤에 붙인다"
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
                              {"item_id": 2, "obj": "どうも", "des": "고마워요", "review": True}], 1643)
    line = [r.getMessage() for r in caplog.records if "표현학습 목록:" in r.getMessage()][-1]
    # 11차 A(2026-09-18): 접두 «…:» 바로 뒤 첫 필드가 call_id — 기존 필드 순서는 그대로다
    assert line == "normalcall 표현학습 목록: call_id=1643 1=ありがとうございます · 2=どうも (2개, 복습 1)"
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



# --------------------------------------------------------------------------- #
# 9차 A (2026-09-16, 1632 #10 どうも) — 유예 판정이 «공개 뒤 복창» 을 passed 로 덮지 않는다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_grace_never_overrides_an_item_already_confirmed_failed(monkeypatch):
    """창 판정에서 failed(정답 공개) 로 확정된 항목은 유예 후보에서 빠지고, 판정기가 passed 라고 해도 기록하지 않는다."""
    seen_extra: list[str] = []

    def verdict(p, s):
        seen_extra.append(s)
        if "[퀴즈 항목]" in s and "2. どうも" in s:                 # 창 판정 — 비버가 정답을 공개했다
            return {"verdicts": [{"num": 2, "verdict": "failed", "why": "선생님이 정답을 알려줌"}]}
        return {"verdicts": [{"num": 2, "verdict": "passed", "why": ""}]}   # 유예 — 복창을 정답으로 본다(막아야 한다)
    fake = FakeJudge(taught_fn=lambda p, s: [], verdict_fn=verdict)
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [2])
    _beaver(st, "«고마워요» 는 일본어로? … It's just どうも. Say it.")
    await _drain(st)
    _user(st, "どうも")
    await _drain(st)
    assert 102 in st.expr_quiz_fail and 102 not in st.expr_quiz_pass, "창 판정: 공개 뒤 복창 → failed"
    assert st.expr_quiz_open is False and st.expr_quiz_grace_from is not None
    _user(st, "どうも")                                            # 유예 창의 학습자 턴 — 또 복창
    await _drain(st)
    assert 102 not in st.expr_quiz_pass and 102 in st.expr_quiz_fail, "유예가 failed 를 passed 로 덮지 않는다"
    grace_instr = seen_extra[-1]
    assert "[세트 밖 항목" not in grace_instr or "2. どうも" not in grace_instr.split("[세트 밖 항목")[1], "failed 확정 항목은 유예 후보에서 뺀다"


@pytest.mark.asyncio
async def test_grace_still_records_a_genuine_spontaneous_answer(monkeypatch):
    """1632 #7·#11 류 — 닫히기 직전 비버가 **묻기만** 한 항목의 자발 정답은 유예에서 그대로 passed."""
    fake = FakeJudge(taught_fn=lambda p, s: [], verdict_fn=lambda p, s: (
        {"verdicts": [{"num": 1, "verdict": "passed"}]} if "1. ありがとうございます" in s.split("[세트 밖 항목")[0]
        else {"verdicts": [{"num": 4, "verdict": "passed", "why": "자발 정답"}]}))
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [1])
    _user(st, "ありがとうございます")
    await _drain(st)
    assert st.expr_quiz_open is False
    _beaver(st, "좋아! 그럼 미안할 때는 일본어로 뭐라고 해?")      # 묻기만 했다(공개 없음)
    await _drain(st)
    _user(st, "ごめんなさい")
    await _drain(st)
    assert 104 in st.expr_quiz_pass and 4 in st.covered_nums



# --------------------------------------------------------------------------- #
# 9차 B (2026-09-16, 1632 블록4 #15) — 퀴즈 중 «새 항목» 을 가르친 것도 세트 이탈
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_teaching_a_brand_new_item_during_a_quiz_also_nudges(monkeypatch):
    fake = FakeJudge(taught_fn=lambda p, s: [5], verdict_fn=lambda p, s: {"verdicts": []})
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    st.covered_nums = [1, 2, 3]
    _open_quiz(st, [1, 2, 3])
    _beaver(st, "자, «네» 는 일본어로 はい 라고 해. 따라 해 봐.")     # 세트 밖 새 항목(5번)을 창 안에서 가르쳤다
    await _drain(st)
    assert 5 in st.covered_nums, "가르침 자체는 그대로 기록"
    assert st.expr_quiz_set_nudge_pending is True, "세트 이탈 — 안내 표시"
    sess = _Sess()
    assert await cs._inject_quiz_set_reminder(sess, st) is True
    assert "지금 낼 문제는" in sess.sent_text_turns[0]



# --------------------------------------------------------------------------- #
# 9차 C (2026-09-16, 1632 계측) — 판정 실패로 문자열 폴백을 탔으면 «폴백» 카운터도 올린다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_failed_taught_judge_counts_both_fail_and_fallback(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger=cs.logger.name)
    fake = FakeJudge(taught_fn=lambda p, s: RuntimeError("boom"))
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _beaver(st, "좋아요! 'ごめんなさい' 도 따라 해 봐요. 미안할 때 쓰는 말이에요.")
    await _drain(st)
    s = st.expr_judge_stats
    assert s["taught_fail"] == 1 and s["taught_fallback"] == 1 and 4 in st.covered_nums
    cs._log_expr_judge_summary(st, 5)
    line = [r.getMessage() for r in caplog.records if "판정 사이드카:" in r.getMessage()][-1]
    assert "가르침 1회(건너뜀 0·실패 1·폴백 1)" in line, line



# --------------------------------------------------------------------------- #
# 10차 (2026-09-16, 1638 — 2.5 가 큐를 무시) — 2.5 면 퀴즈 큐를 완결 텍스트 턴으로 · 3.1 종전 · 비버 발화 중엔 안 보냄
# --------------------------------------------------------------------------- #
class _CueSess:
    def __init__(self):
        self.text_turns: list[str] = []
        self.regrounds: list[tuple[str, bool]] = []

    async def send_text_turn(self, text: str) -> None:
        self.text_turns.append(text)

    async def send_reground(self, text: str, *, turn_complete: bool = True) -> None:
        self.regrounds.append((text, turn_complete))


def _armed_cue_state(model):
    st = _state()
    st.live_model = model
    st.expr_quiz_cue_pending = "[큐] 퀴즈 1·2·3"          # 큐 arm 은 다른 시험이 지킨다 — 여기선 보내는 통로만 본다
    st.expr_quiz_set, st.expr_quiz_seq = [1, 2, 3], 1
    st.expr_quiz_prev_num = None                          # 보류 항목 없음 → 정리 대기 통과
    return st


@pytest.mark.asyncio
async def test_quiz_cue_is_a_completed_text_turn_on_25(caplog):
    import logging
    caplog.set_level(logging.INFO, logger=cs.logger.name)
    st = _armed_cue_state("gemini-live-2.5-flash-native-audio")
    sess = _CueSess()
    await cs._attach_quiz_cue(sess, st, "마이크")
    assert len(sess.text_turns) == 1 and sess.regrounds == [], "2.5 → send_text_turn(완결 턴)"
    assert st.expr_quiz_cue_pending is None and st.expr_quiz_awaiting_open is True
    line = [r.getMessage() for r in caplog.records if "퀴즈 큐 얹기:" in r.getMessage()][-1]
    assert "tc=True 모델=gemini-live-2.5-flash-native-audio" in line


@pytest.mark.asyncio
async def test_quiz_cue_stays_an_incomplete_attach_on_31_and_when_switched_off(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger=cs.logger.name)
    st = _armed_cue_state("gemini-3.1-flash-live-preview")
    sess = _CueSess()
    await cs._attach_quiz_cue(sess, st, "마이크")
    assert sess.text_turns == [] and len(sess.regrounds) == 1 and sess.regrounds[0][1] is False, "3.1 → 종전 tc=False"
    assert "tc=False" in [r.getMessage() for r in caplog.records if "퀴즈 큐 얹기:" in r.getMessage()][-1]
    monkeypatch.setattr(cs._settings, "EXPR_CUE_COMPLETED_TURN_25", False)
    st2 = _armed_cue_state("gemini-live-2.5-flash-native-audio")
    sess2 = _CueSess()
    await cs._attach_quiz_cue(sess2, st2, "마이크")
    assert sess2.text_turns == [] and sess2.regrounds[0][1] is False, "스위치 끄면 2.5 도 종전"
    st3 = _armed_cue_state(None)
    sess3 = _CueSess()
    await cs._attach_quiz_cue(sess3, st3, "마이크")
    assert sess3.text_turns == [] and len(sess3.regrounds) == 1, "모델을 모르면(레벨테스트·시험) 종전"


@pytest.mark.asyncio
async def test_completed_turn_cue_is_not_sent_while_the_beaver_is_speaking():
    st = _armed_cue_state("gemini-live-2.5-flash-native-audio")
    st.turn_id = "t-speaking"
    sess = _CueSess()
    await cs._attach_quiz_cue(sess, st, "마이크")
    assert sess.text_turns == [] and sess.regrounds == [] and st.expr_quiz_cue_pending is not None, "R4 — 발화 중엔 보류(다음 관문에서 다시)"
    st.turn_id = None
    await cs._attach_quiz_cue(sess, st, "마이크")
    assert len(sess.text_turns) == 1


# --------------------------------------------------------------------------- #
# 11차 A (2026-09-18, 1643 ja 하네스 ↔ 1644 ko 사장님이 같은 시간대에 돌아 하네스가 서로의 줄을 섞어 읽었다) — 표현학습 로그 줄마다 call_id
# --------------------------------------------------------------------------- #
# 11차 A — 하네스가 읽는 줄의 «접두 뒤 첫 필드» 형식. 소스의 포맷 문자열에서 이 모양이 유지되는지 시험이 지킨다(줄을 실행 못 하는 것까지).
CID_FORMATS = (
    "표현학습 목록: call_id=%s",
    " arm: call_id=%s", " 보류: call_id=%s", " 얹기: call_id=%s", " 열림: call_id=%s", " 강제 닫힘: call_id=%s",
    " 미얹힘(통화 끝): call_id=%s", " 얹기 실패(다음 발화 재시도): call_id=%s",
    " 세트 이탈: call_id=%s", " 세트 이탈(안내 상한 %d): call_id=%s", " 세트 안내 주입 %d/%d: call_id=%s",
    "퀴즈 판정(LLM): call_id=%s", "퀴즈 판정(LLM·세트 밖): call_id=%s", "유예 판정 기각(이미 오답 확정): call_id=%s",
    "드릴 루프 감지: call_id=%s", "드릴 안내 주입 %d/%d: call_id=%s",
)


class _CidSess:
    def __init__(self):
        self.text_turns: list[str] = []

    async def send_text_turn(self, text: str) -> None:
        self.text_turns.append(text)

    async def send_reground(self, text: str, *, turn_complete: bool = True) -> None:
        self.text_turns.append(text)


def _cid_state(call_id=1643):
    st = _state()
    st.call_id = call_id
    return st


@pytest.mark.asyncio
async def test_every_expression_log_line_carries_the_call_id(caplog):
    """A — 하네스가 통화를 구분할 수 있게 지정된 줄 전부에 call_id 가 실린다."""
    import logging
    caplog.set_level(logging.INFO, logger=cs.logger.name)
    st = _cid_state()
    cs._log_expression_items([{"item_id": 1, "obj": "ありがとうございます"}], st.call_id)
    st.covered_nums = [1, 2, 3]
    st.expr_quiz_cue_pending = "[큐]"
    st.expr_quiz_set, st.expr_quiz_seq, st.expr_quiz_prev_num = [1, 2, 3], 1, None
    sess = _CidSess()
    await cs._attach_quiz_cue(sess, st, "마이크")                 # 얹기
    st.expr_quiz_awaiting_open = True
    cs._expression_quiz_open_on_beaver_turn(st)                   # 열림
    st.expr_quiz_open_user_turns = cs.EXPR_QUIZ_OPEN_MAX_USER_TURNS - 1
    cs._expression_quiz_note_open_user_turn(st, "음 모르겠어요")    # 강제 닫힘
    # 세트 이탈 · 세트 안내 주입
    st.expr_quiz_open, st.expr_quiz_set = True, [1, 2]
    st.covered_nums = [1, 2, 3]
    cs._note_quiz_set_drift(st, [3], 7)
    await cs._inject_quiz_set_reminder(sess, st)
    # 세트 밖 통과 · 유예 기각
    cs._apply_off_set_pass(st, 3, "자발 정답")
    st.expr_quiz_fail.add(st.expr_items[0]["item_id"])
    # 보류 줄
    st2 = _cid_state()
    st2.expr_quiz_cue_pending, st2.expr_quiz_prev_num, st2.expr_quiz_cue_user_turns = "[큐]", 1, 0
    await cs._attach_quiz_cue(_CidSess(), st2, "마이크")
    msgs = [r.getMessage() for r in caplog.records]
    for mark in ("표현학습 목록:", "퀴즈 큐 얹기:", "퀴즈 큐 열림:", "퀴즈 큐 강제 닫힘:", "퀴즈 큐 세트 이탈:",
                 "세트 안내 주입", "퀴즈 판정(LLM·세트 밖):", "퀴즈 큐 보류:"):
        hits = [m for m in msgs if mark in m]
        assert hits, "이 줄이 안 찍혔다: %s" % mark
        assert all("call_id=1643" in m for m in hits), (mark, hits)


@pytest.mark.asyncio
async def test_call_id_is_the_first_field_after_the_prefix_and_old_fields_keep_their_order(caplog):
    """A — 하네스 파서용 형식: «…: call_id=NNNN <종전 첫 필드> …» (call_id 만 끼워 넣고 뒤 필드 순서는 그대로) · call_id 가 없으면 «-»."""
    import io
    import logging
    import re
    caplog.set_level(logging.INFO, logger=cs.logger.name)
    st = _cid_state(1644)
    st.expr_quiz_cue_pending, st.expr_quiz_set, st.expr_quiz_seq, st.expr_quiz_prev_num = "[큐]", [4, 5], 2, None
    st.live_model = "gemini-live-2.5-flash-native-audio"
    await cs._attach_quiz_cue(_CidSess(), st, "마이크")
    line = [r.getMessage() for r in caplog.records if "퀴즈 큐 얹기:" in r.getMessage()][-1]
    assert re.search(r"퀴즈 큐 얹기: call_id=1644 seq=2 항목=\[4, 5\] 얹기=마이크 대기=\d+s 비버턴=.+ 정리=.* tc=\w+ 모델=", line), line
    m = re.search(r"call_id=(\d+|-)", line)
    assert m and m.group(1) == "1644"
    # 지정된 줄 전부 — 소스의 포맷 문자열이 «접두 뒤 첫 필드 = call_id» 를 지킨다
    src = io.open(cs.__file__, encoding="utf-8").read()
    for fmt in CID_FORMATS:
        assert fmt in src, "이 줄에 call_id 가 빠졌다: %s" % fmt
    # call_id 를 모르는 경로(레벨테스트·시험)는 «-»
    caplog.clear()
    st0 = _state()
    st0.expr_quiz_cue_pending, st0.expr_quiz_set, st0.expr_quiz_seq, st0.expr_quiz_prev_num = "[큐]", [1], 1, None
    await cs._attach_quiz_cue(_CidSess(), st0, "마이크")
    assert "call_id=- seq=1" in [r.getMessage() for r in caplog.records if "퀴즈 큐 얹기:" in r.getMessage()][-1]


# --------------------------------------------------------------------------- #
# 11차 B (2026-09-18, 1643 2.5 드릴 루프 — 한 항목 6~8턴 · 통과 항목 재드릴 3건) — «다음 번호 항목으로» 안내
# --------------------------------------------------------------------------- #
JA_DRILL = [
    {"item_id": 101, "obj": "こんにちは", "des": "안녕하세요", "ex": None},
    {"item_id": 102, "obj": "はじめまして", "des": "처음 뵙겠습니다", "ex": None},
    {"item_id": 103, "obj": "ありがとうございます", "des": "감사합니다", "ex": None},
]


def _drill_state(items=None):
    st = _state(items=items or JA_DRILL)
    st.call_id = 1643
    st.expr_llm_judge = False          # 문자열 경로 — 드릴 추적은 판정기와 무관하다
    return st


async def _turn_end(sess, st):
    """turn_end 자리의 주입 관문(세트 안내 → 드릴 안내 택일)만 흉내낸다."""
    if st.expr_quiz_set_nudge_pending and st.turn_id is None and not st.should_close:
        await cs._inject_quiz_set_reminder(sess, st)
    elif st.expr_drill_nudge_pending and st.turn_id is None and not st.should_close:
        await cs._inject_drill_move_on(sess, st)


@pytest.mark.asyncio
async def test_drill_on_one_item_past_three_learner_turns_gets_one_move_on_note(caplog):
    """B① — 같은 항목을 붙잡고 학습자 턴 3회를 넘기면 안내 1회(문장이 달라 루프 차단기는 안 걸리는 경우)."""
    import logging
    caplog.set_level(logging.INFO, logger=cs.logger.name)
    st, sess = _drill_state(), _CidSess()
    _beaver(st, "«こんにちは» 따라 해 보세요")
    assert st.expr_drill_focus == 1
    for i, said in enumerate(("こんにちは", "곤니치와", "こんにちは?", "다시 해볼게요"), 1):
        _user(st, said)
        _beaver(st, "좋아요! 한 번 더 — «こんにちは» 를 크게 말해 보세요 (%d)" % i)   # 매번 다른 문장 = 루프 차단기 미발동
        await _turn_end(sess, st)
    assert sess.text_turns == [cs.EXPRESSION_DRILL_MOVE_ON], "3턴 초과 시점에 안내 1회"
    assert st.expr_drill_nudges == 1 and st.expr_drill_nudge_pending is False
    line = [r.getMessage() for r in caplog.records if "드릴 안내 주입" in r.getMessage()][-1]
    assert "call_id=1643" in line and "1/6" in line


JA_DRILL8 = [{"item_id": 200 + i, "obj": obj, "des": "뜻%d" % i, "ex": None} for i, obj in enumerate(
    ("こんにちは", "はじめまして", "ありがとうございます", "すみません", "おはようございます",
     "こんばんは", "どういたしまして", "さようなら"), 1)]


@pytest.mark.asyncio
async def test_drill_move_on_note_is_sent_once_per_item():
    """D① — 같은 항목에는 두 번 안내하지 않는다(1645: 한 항목이 앞 2분에 상한 3 을 다 먹었다)."""
    st, sess = _drill_state(), _CidSess()
    _beaver(st, "«こんにちは» 따라 해 보세요")
    for i in range(8):                                   # 같은 항목을 계속 붙잡는다
        _user(st, "음... %d" % i)
        _beaver(st, "한 번 더 — «こんにちは» (%d)" % i)
        await _turn_end(sess, st)
    assert len(sess.text_turns) == 1 and st.expr_drill_nudges == 1, sess.text_turns
    assert st.expr_drill_nudged == {1}
    # 다른 항목으로 옮기면 그 항목에는 다시 쓸 수 있다
    _beaver(st, "이제 «はじめまして» 예요")
    for i in range(5):
        _user(st, "네 %d" % i)
        _beaver(st, "«はじめまして» 다시 (%d)" % i)
        await _turn_end(sess, st)
    assert len(sess.text_turns) == 2 and st.expr_drill_nudged == {1, 2}


@pytest.mark.asyncio
async def test_drill_move_on_note_is_capped_at_six_per_call():
    """D② — 통화당 상한 6회(11차의 3회는 후반 루프에 개입을 0 으로 만들었다)."""
    st, sess = _drill_state(JA_DRILL8), _CidSess()
    st.covered_nums = list(range(1, 9))                  # 전부 다뤘다 — 되돌아간 재드릴이 항목마다 1회씩 잡힌다
    st.expr_drill_focus = 8
    for n, obj in enumerate([d["obj"] for d in JA_DRILL8][:7], 1):
        _beaver(st, "«%s» 를 다시 연습해 봐요" % obj)
        await _turn_end(sess, st)
        _beaver(st, "«さようなら» 로 돌아가죠")            # 초점을 마지막 covered 로 되돌려 다음 항목이 또 잡히게
        await _turn_end(sess, st)
    assert cs.EXPR_DRILL_NUDGE_MAX == 6
    assert len(sess.text_turns) == 6 == st.expr_drill_nudges, sess.text_turns
    assert len(st.expr_drill_nudged) == 6, st.expr_drill_nudged


@pytest.mark.asyncio
async def test_normal_drill_progression_gets_no_move_on_note():
    """B③ — 정상 드릴(항목마다 2~3턴 주고 다음 번호로)엔 안내 0회 · 퀴즈 창이 열린 동안도 0회(그 구간은 세트 안내 몫)."""
    st, sess = _drill_state(), _CidSess()
    for n, label in ((1, "こんにちは"), (2, "はじめまして"), (3, "ありがとうございます")):
        _beaver(st, "«%s» 따라 해 보세요" % label)
        _user(st, label)
        _beaver(st, "잘했어요!")
        _user(st, "네")
        await _turn_end(sess, st)
        assert st.expr_drill_focus == n
    assert sess.text_turns == [] and st.expr_drill_nudges == 0
    # 퀴즈 창이 열리면 드릴 추적은 멈춘다(주입 겹침 금지)
    st.expr_quiz_open, st.expr_quiz_set = True, [1, 2, 3]
    st.expr_drill_focus, st.expr_drill_user_turns = 1, 0
    for i in range(5):
        _beaver(st, "«こんにちは» 는 뭐였죠? (%d)" % i)
        _user(st, "음...")
        await _turn_end(sess, st)
    assert sess.text_turns == [] and st.expr_drill_nudge_pending is False


# --------------------------------------------------------------------------- #
# 12차 C (2026-09-18, 1646 #7 ◯◯から来ました — 유예 창에서 판정기가 세트 밖 칸을 통째로 건너뛰어 자발 정답 기록 0)
#   1645 #13 은 같은 모양인데 기록됐다 ⇒ 모델 재량이었다. 서버 대조로 줍는다(관통원칙 ① AI 는 증인·판정은 코드).
# --------------------------------------------------------------------------- #
JA_FROM = [
    {"item_id": 301, "obj": "はじめまして", "des": "처음 뵙겠습니다", "ex": None},
    {"item_id": 302, "obj": "◯◯です", "des": "저는 ◯◯입니다", "ex": None},
    {"item_id": 303, "obj": "◯◯から来ました", "des": "◯◯에서 왔습니다", "ex": None},
]


def _grace_fake(monkeypatch, in_window_pass=2):
    """창 판정은 세트를 확정(→ 닫힘 → 유예 창) · 유예 판정은 1646 처럼 verdicts 0건으로 답한다."""
    def verdict(prompt, system):
        main = system.split("[퀴즈 항목]")[1].split("[세트 밖 항목")[0]
        if "(없음)" in main:                       # 유예 — 세트가 이미 확정돼 본 칸이 비었다
            return {"verdicts": []}
        return {"verdicts": [{"num": in_window_pass, "verdict": "passed", "why": "자발 정답"}]}
    fake = FakeJudge(taught_fn=lambda p, s: [], verdict_fn=verdict)
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    return fake


@pytest.mark.asyncio
async def test_grace_off_set_pass_is_recovered_by_the_server_when_the_judge_skips_it(monkeypatch):
    """C① — 유예 판정이 verdicts 0건으로 와도, 창 안에서 학습자가 세트 밖 항목을 스스로 말했으면 서버가 통과로 적는다(1646 #7)."""
    _grace_fake(monkeypatch)
    st = _state(items=JA_FROM)
    st.call_id = 1646
    _open_quiz(st, [2])
    _beaver(st, "«저는 존입니다» 는 일본어로 어떻게 말해요?")
    await _drain(st)
    _user(st, "存です")                                  # 세트 항목(2) 자발 정답 → 세트 전부 확정 → 창 닫힘
    await _drain(st)
    assert st.expr_quiz_open is False and st.expr_quiz_grace_from is not None, "유예 창이 열려 있다(전제)"
    _beaver(st, "Now, back to the real stuff. How do you say «I am from New York»? Try that.")   # 세트 밖 #3 을 영어로만 묻는다
    _user(st, "韓国から来ました。")                        # ◯◯ 자리표시를 채운 정답
    await _drain(st)
    assert 303 in st.expr_quiz_pass, "세트 밖 자발 정답이 기록되지 않았다(1646 #7 재발)"
    assert 3 in st.covered_nums and 3 in st.expr_quizzed, "passed + covered + 출제됨"


@pytest.mark.asyncio
async def test_server_recovery_does_not_record_a_parrot_after_the_reveal(monkeypatch):
    """C② — 비버가 그 표현을 **먼저** 말한 자리(공개 뒤 복창)면 서버 대조도 기록하지 않는다(5차 A-2·9차 A 규율 그대로)."""
    _grace_fake(monkeypatch)
    st = _state(items=JA_FROM)
    _open_quiz(st, [2])
    _beaver(st, "«저는 존입니다» 는 일본어로?")
    await _drain(st)
    _user(st, "存です")
    await _drain(st)
    assert st.expr_quiz_grace_from is not None
    _beaver(st, "It's ◯◯から来ました. Say it!")            # 공개
    _user(st, "韓国から来ました。")                        # 복창
    await _drain(st)
    assert 303 not in st.expr_quiz_pass and 3 not in st.expr_quizzed, "공개 뒤 복창은 통과가 아니다"
