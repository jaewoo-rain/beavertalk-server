# -*- coding: utf-8 -*-
"""T14 — 판정 프롬프트 재설계 + 코드 4건 회귀 (2026-09-11, 외부 의존 0).

## 왜 — 첫 실통화(1397, 사장님 316초)에서 판정이 네 겹으로 틀렸다
    ① 앵무새를 통과로 셈          ② 드릴 산출을 퀴즈 통과로 셈(첫 퀴즈 전에 통과)
    ③ failed 구조적으로 0         ④ 마지막 판정 1,201ms > 상한 → 마지막 145초 미판정
⇒ 엄격히 진짜 통과는 1개인데 DB 는 3개. 사장님 재정의: «배웠는가 / 맞췄는가».

## ⛔ 이 파일이 재는 것과 재지 않는 것
판정은 LLM 이 한다 — **LLM 결과를 직접 시험하려 들지 마라**(그건 실통화 몫이다).
여기서 잠그는 것은 두 가지다:
  · 지시문에 **확정 규칙 문장**이 들어 있다(사장님 확정본 — 뜻을 바꾸면 여기서 터진다)
  · 서버 쪽 적용 규칙(단조성 · 구간 보류 · 입력 축소 · phase 보존 · 자리표시 예외)
  · **목록에 서버 사실이 붙는다**(반려 P1 — 잘린 전사에서 «처음 나오는 항목=드릴» 이 이기지 않게)
1397 전사는 픽스처로 박아 **기대 판정**(drilled={1..7} · passed={6} · failed={1,2,3,4,5})을
문서화한다 — 실통화 재측정 때 같은 전사로 비교할 기준선이다.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

import domains.learning.realtime.call_session as cs
from core.prompts.expression import (
    NUDGE_SEED_1_EXPRESSION,
    build_expression_instruction,
    build_expression_reground_brief,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "call1397_transcript.txt"

# 1397 에서 다룬 7항목(전사 순서). 기대 판정은 사장님 재정의 기준(결정로그 §4-D·§4-E).
ITEMS_1397 = [
    {"item_id": 1, "obj": "이거 얼마예요?", "des": "How much is it?", "ex": None},
    {"item_id": 2, "obj": "잘 부탁드립니다", "des": "Please take good care of me", "ex": None},
    {"item_id": 3, "obj": "저는 ◯◯ 사람이에요", "des": "I'm from ◯◯", "ex": None},
    {"item_id": 4, "obj": "도와주세요", "des": "Please help me", "ex": None},
    {"item_id": 5, "obj": "처음 뵙겠습니다", "des": "How do you do?", "ex": None},
    {"item_id": 6, "obj": "◯◯이/가 뭐예요?", "des": "What is ◯◯?", "ex": None},
    {"item_id": 7, "obj": "네", "des": "yes / I see", "ex": None},
]
EXPECTED_1397 = {"drilled": {1, 2, 3, 4, 5, 6, 7}, "passed": {6}, "failed": {1, 2, 3, 4, 5}}

BASE = dict(
    role="비버 선생님", personality="직설적", level_profile="아주 쉬운 문장",
    locale="en", interests=["축구"], name="Tester", target_language="한국어",
)


def _instr() -> str:
    return cs._expression_progress_instruction(ITEMS_1397, "한국어", "영어(English)")


def _script() -> str:
    return build_expression_instruction(**BASE, items=ITEMS_1397, quiz_group=3)


def _state() -> cs._CallState:
    st = cs._CallState()
    st.expr_items = list(ITEMS_1397)
    st.reground_items = [i["obj"] for i in ITEMS_1397]
    return st


# --------------------------------------------------------------------------- #
# 픽스처 — 기준선 문서화
# --------------------------------------------------------------------------- #
def test_the_1397_transcript_fixture_is_present_and_readable() -> None:
    """실통화 재측정의 **비교 기준선**이다 — 같은 전사를 새 지시문에 넣어 기대 판정과 비교한다."""
    text = FIXTURE.read_text(encoding="utf-8")
    assert "quiz time" in text.lower()                 # 두 번째 퀴즈 앵커(t34)
    assert "let's see if you remember" in text.lower()  # 첫 퀴즈 앵커(t14)
    assert "[Country]" in text                          # C4 가 잡던 자리표시
    # ⚠ 반려 P2-3: 첫 합본이 결손본이었다 — 항목3 «공개→복창» 회차(t25→t26→t27)가 빠져 있었다.
    assert "USER[t26]: 저는 미국 사람이에요." in text
    assert "Awesome, you nailed it! Okay, new phrase." in text
    assert EXPECTED_1397["passed"] == {6}


# --------------------------------------------------------------------------- #
# A. 판정기 지시문 — 확정 문장이 그대로 들어 있다
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "rule",
    [
        # 두 축
        "판정은 두 가지뿐이다: **배웠는가(drilled)** / **맞췄는가(passed·failed)**.",
        # 구간
        "먼저 구간을 가려라",
        "**앵커는 단어가 아니라 뜻으로 판단한다**",
        "같은 항목을 가르치던 중의 «다시 해봐» 는 드릴 재시도다 — 퀴즈가 아니다",
        "알림 뒤라도 **처음 나오는 항목**은 드릴이다",
        "그래도 모호하면 **그 항목만** passed·failed 를 비워라",
        "«오답 재출제» 도 퀴즈다",
        # drilled
        "■ drilled (배웠는가) — 구간과 무관하다.",
        "학습자가 몰라서 선생님이 알려주고 학습자가 따라 말한 것도 **배운 것이다**",
        "**드릴 정답은 passed 가 아니다** — 아직 퀴즈를 안 봤다",
        # passed
        "■ passed (맞췄는가) — **퀴즈 구간에서만** 판정한다",
        "선생님이 정답을 말해 주기 **전에** 학습자가 스스로 그 표현을 냈으면 passed",
        "드릴 때 들려준 것은 공개로 치지 않는다",
        "힌트를 받고 맞힌 것도 passed 다",
        # failed
        "■ failed — **퀴즈 구간에서만** 판정한다.",
        "공개 뒤 학습자가 따라 말했어도 **failed 그대로다**",
        "⛔ 침묵은 오답이 아니다",
        # 격식
        "목표 표현이 존댓말인데 학습자가 반말로 냈으면 **선생님이 맞았다고 해도 passed 가 아니다.**",
        "격식 표지가 사라지면 불허",
        "«Good, but / Almost / Just add» 는 정답 반응이 아니다",
        # 재출제·단조
        "**passed 를 failed 로 되돌리지는 마라.**",
        "passed 와 failed 는 한 항목에 하나만. drilled 는 둘과 함께 있을 수 있다.",
        "목록에 없는 번호를 지어내지 마라",
    ],
)
def test_the_judge_instruction_carries_each_confirmed_rule(rule: str) -> None:
    """⛔ 사장님 확정본(「응 이대로」). 뜻을 바꾸면 여기서 터진다 — 그게 이 표의 임무다."""
    assert rule in _instr(), f"확정 규칙이 빠졌다: {rule}"


def test_the_judge_instruction_speaks_in_the_learners_language() -> None:
    """앵커·설명·질문의 언어는 학습자 모국어다 — 라벨이 실제로 치환돼야 한다."""
    out = _instr()
    assert "영어(English)로 퀴즈·복습·테스트를 알리는 말" in out
    assert "{locale_label}" not in out and "{L}" not in out


def test_the_judge_instruction_lists_items_with_meaning() -> None:
    """동음이의(91그룹)를 가르려면 뜻이 같이 실려야 한다 — 이건 T14 이전 계약 그대로다."""
    out = _instr()
    assert "1. 이거 얼마예요? — 뜻: How much is it?" in out
    assert "7. 네 — 뜻: yes / I see" in out


def test_the_judge_instruction_asks_for_phase() -> None:
    """C2·C3 재료 — 마지막 구간을 돌려받아야 절단 시 보존하고 앵커 누락을 잴 수 있다."""
    assert '드릴이면 "drill", 퀴즈면 "quiz", 모호하면 빈 문자열' in _instr()


# --------------------------------------------------------------------------- #
# 서버 적용 규칙 — 단조성 · 구간 보류
# --------------------------------------------------------------------------- #
class _Out:
    def __init__(self, drilled=(), passed=(), failed=(), phase=""):
        self.drilled, self.passed, self.failed, self.phase = (
            list(drilled), list(passed), list(failed), phase,
        )


def test_passed_is_never_demoted_to_failed_on_the_server() -> None:
    """⛔ 지시문이 «되돌리지 마라» 라고 해도 **서버가 한 번 더** 지킨다(append-only).

    LLM 이 어긴 결과가 와도 서버에서 강등이 일어나지 않는다 — 두 겹이다.
    """
    st = _state()
    cs._apply_expression_progress(st, _Out(passed=[6]))
    cs._apply_expression_progress(st, _Out(failed=[6]))          # LLM 이 규칙을 어겼다
    assert 6 in st.expr_quiz_pass and 6 not in st.expr_quiz_fail


def test_a_failed_item_may_later_become_passed() -> None:
    """재출제에서 맞으면 passed 로 **올라간다** — 반대 방향만 막는다."""
    st = _state()
    cs._apply_expression_progress(st, _Out(failed=[1]))
    cs._apply_expression_progress(st, _Out(passed=[1]))
    assert 1 in st.expr_quiz_pass and 1 not in st.expr_quiz_fail


def test_an_ambiguous_item_left_empty_changes_nothing() -> None:
    """구간 보류 — 판정기가 «그 항목만 비워라» 를 따르면 서버 상태도 그대로다."""
    st = _state()
    cs._apply_expression_progress(st, _Out(drilled=[1, 2], passed=[], failed=[]))
    assert st.expr_quiz_pass == set() and st.expr_quiz_fail == set()
    assert cs._expr_covered_ids(st) == [1, 2]


# --------------------------------------------------------------------------- #
# P1 — 목록에 서버가 아는 사실이 붙는다 (이미 드릴함 · 통과 · 오답)
# --------------------------------------------------------------------------- #
def test_the_listing_carries_server_facts() -> None:
    """⛔ 잘린 전사엔 앞서 드릴한 흔적이 없다 — 표시가 없으면 판정기가 «처음 나오는 항목=드릴»
    로 읽어 마지막 퀴즈의 통과가 사라진다. 그래서 서버가 아는 것을 항목 옆에 붙인다.
    """
    out = cs._expression_progress_instruction(
        ITEMS_1397, "한국어", "영어(English)",
        covered_nums=[1, 2, 3], passed_ids={2}, failed_ids={1},
    )
    lines = {l.split(". ", 1)[0]: l for l in out.splitlines() if l[:1].isdigit()}
    assert lines["1"].endswith("(이미 드릴함 · 오답)")
    assert lines["2"].endswith("(이미 드릴함 · 통과)")
    assert lines["3"].endswith("(이미 드릴함)")
    assert "(" not in lines["4"].split("— 뜻:")[-1], "안 다룬 항목엔 표시가 없어야 한다"
    assert "표시는 서버가 확인한 사실이다" in out
    assert "이미 배운 것을 다시 묻는 것**이다" in out


def test_a_pass_or_fail_implies_drilled_even_without_covered_num() -> None:
    """통과·오답은 퀴즈를 봤다는 뜻이다 — covered_nums 에 없어도 «이미 드릴함» 이 붙는다."""
    out = cs._expression_progress_instruction(
        ITEMS_1397, "한국어", "영어(English)", passed_ids={6},
    )
    assert "6. ◯◯이/가 뭐예요? — 뜻: What is ◯◯?  (이미 드릴함 · 통과)" in out


def test_the_first_time_rule_yields_to_the_server_mark() -> None:
    """«처음 나오는 항목은 드릴» 규칙 문장 자체에 예외가 달려 있다 — 표시가 이긴다."""
    out = _instr()
    assert "알림 뒤라도 **처음 나오는 항목**은 드릴이다 — 단, 목록에 (이미 드릴함) 표시가 있으면 예외다" in out


@pytest.mark.asyncio
async def test_the_judge_instruction_is_rebuilt_from_state_on_every_call(monkeypatch) -> None:
    """⛔ 통화 시작에 구운 고정 지시문을 쓰면 표시가 영원히 비어 있다 — 판정마다 다시 조립한다.

    재현(bt-back): arm 이 항목 4~6 드릴 **직후**에 서고 → 판정 → 커서 이동 → 퀴즈2(4·5·6) → 끝.
    마지막 입력엔 퀴즈2 만 있다. 목록에 4·5·6 이 «이미 드릴함» 으로 실려야 통과가 살아남는다.
    """
    st = _state()
    st.expr_ctx = {"client": object(), "model": "m", "target_language": "한국어",
                   "locale_label": "영어(English)"}
    st.covered_nums = [1, 2, 3, 4, 5, 6]
    st.expr_covered_snapshot = [1, 2, 3, 4, 5, 6]        # 직전 판정 완료 시점 스냅샷(T15-4) — 표시의 원본
    st.expr_quiz_pass = {2}
    st.expr_quiz_fail = {1, 3}
    st.segments = [{"turn_index": i, "role": "user", "text": f"발화{i}"} for i in range(16)]
    st.expr_judged_upto = 13

    seen: dict = {}

    async def _capture(client, model, **kw):
        seen["instruction"] = kw.get("system_instruction", "")
        seen["prompt"] = kw.get("prompt", "")
        return _Out(phase="quiz")

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _capture)
    await cs._final_expression_progress(st)
    assert "발화8" not in seen["prompt"], "입력은 잘렸다(C1 — 커서 13, 겹침 4 → 9부터)"
    ins = seen["instruction"]
    assert "4. 도와주세요 — 뜻: Please help me  (이미 드릴함)" in ins
    assert "5. 처음 뵙겠습니다 — 뜻: How do you do?  (이미 드릴함)" in ins
    assert "6. ◯◯이/가 뭐예요? — 뜻: What is ◯◯?  (이미 드릴함)" in ins
    assert "(이미 드릴함 · 통과)" in ins and "(이미 드릴함 · 오답)" in ins
    assert "7. 네 — 뜻: yes / I see" in ins and "7. 네 — 뜻: yes / I see  (" not in ins


def test_the_judge_instruction_falls_back_to_the_expression_ctx_labels() -> None:
    st = _state()
    st.expr_ctx = {"client": object(), "model": "m", "target_language": "한국어",
                   "locale_label": "영어(English)"}
    ins = cs._expression_judge_instruction(st)
    assert ins.startswith("너는 한국어 표현학습 통화의 진도 판정기다")
    assert "영어(English)로 퀴즈·복습·테스트를 알리는 말" in ins


# --------------------------------------------------------------------------- #
# C1 — 마지막 판정 입력은 «마지막 통화중 판정 이후 구간» 만
# --------------------------------------------------------------------------- #
def test_the_transcript_can_start_from_a_cursor() -> None:
    st = _state()
    st.segments = [
        {"turn_index": i, "role": "user", "text": f"발화{i}"} for i in range(6)
    ]
    out = cs._expression_transcript(st, since=4)
    assert "발화4" in out and "발화5" in out
    assert "발화3" not in out and "발화0" not in out


@pytest.mark.asyncio
async def test_the_final_judgement_only_sends_the_unjudged_tail_plus_overlap(monkeypatch) -> None:
    """⛔ 전사 전체를 넣으면 새 지시문(옛것의 2배) 아래서 1397 처럼 상한을 넘긴다
    (1,201ms → 마지막 145초 미판정). 앞 구간 결과는 state 에 합집합으로 이미 있다.
    ⭐ 겹침 4 세그먼트(bt-back 결정): 경계에 걸친 질문/답 쌍을 붙여 준다 — 커서=9 → 입력은 5부터.
    """
    st = _state()
    st.expr_ctx = {"client": object(), "model": "m", "instruction": "i"}
    st.segments = [{"turn_index": i, "role": "user", "text": f"발화{i}"} for i in range(12)]
    st.expr_judged_upto = 9                            # 통화 중 판정이 9개까지 봤다

    seen: dict = {}

    async def _capture(client, model, **kw):
        seen["prompt"] = kw.get("prompt", "")
        return _Out(phase="quiz")

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _capture)
    await cs._final_expression_progress(st)
    assert all(f"발화{i}" in seen["prompt"] for i in range(5, 12)), "커서 이후 + 겹침 4"
    assert "발화4" not in seen["prompt"], "겹침보다 앞이 다시 들어갔다"
    assert st.expr_judged_upto == 12, "커서는 «본 끝» 까지 — 겹침이 커서를 뒤로 돌리지 않는다"


@pytest.mark.asyncio
async def test_a_mid_call_judgement_uses_the_same_window_as_the_final_one(monkeypatch) -> None:
    """⭐ 통화중 판정도 «커서 이후 + 겹침» 이다 — 마지막만 잘라 보내면 두 경로가 다른 판정기가 된다.
    12k 절단이 실제로 걸리던 자리가 통화중(9분 근처)이었다 — 이제 사실상 안 걸린다.
    """
    st = _state()
    st.expr_ctx = {"client": object(), "model": "m", "instruction": "i"}
    st.segments = [{"turn_index": i, "role": "user", "text": f"발화{i}"} for i in range(12)]
    st.expr_judged_upto = 9
    seen: dict = {}

    async def _capture(client, model, **kw):
        seen["prompt"] = kw.get("prompt", "")
        return _Out(phase="drill")

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _capture)
    await cs._expression_progress_sidecar(st)
    assert "발화5" in seen["prompt"] and "발화4" not in seen["prompt"]
    assert st.expr_judged_upto == 12


@pytest.mark.asyncio
async def test_cursor_zero_means_from_the_start_without_overlap(monkeypatch) -> None:
    st = _state()
    st.expr_ctx = {"client": object(), "model": "m", "instruction": "i"}
    st.segments = [{"turn_index": i, "role": "user", "text": f"발화{i}"} for i in range(3)]
    seen: dict = {}

    async def _capture(client, model, **kw):
        seen["prompt"] = kw.get("prompt", "")
        return _Out()

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _capture)
    await cs._expression_progress_sidecar(st)
    assert seen["prompt"].endswith("학습자: 발화0" + chr(10) + "학습자: 발화1" + chr(10) + "학습자: 발화2")
    assert "[앞 구간 생략" not in seen["prompt"]


@pytest.mark.asyncio
async def test_rejudging_the_overlap_never_demotes_a_pass(monkeypatch) -> None:
    """겹친 구간을 두 번 판정해도 무해하다 — 단조성(failed→passed 만)이라 재판정이 강등을 못 만든다."""
    st = _state()
    st.expr_ctx = {"client": object(), "model": "m", "instruction": "i"}
    st.segments = [{"turn_index": i, "role": "user", "text": f"발화{i}"} for i in range(6)]
    verdicts = iter([_Out(drilled=[2], passed=[2], phase="quiz"), _Out(drilled=[2], failed=[2], phase="quiz")])

    async def _next(client, model, **kw):
        return next(verdicts)

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _next)
    await cs._expression_progress_sidecar(st)          # 1차: 2번 통과, 커서 6
    st.segments += [{"turn_index": i, "role": "user", "text": f"발화{i}"} for i in range(6, 8)]
    await cs._expression_progress_sidecar(st)          # 2차: 겹침(2~5) 포함 재판정이 2번을 오답이라 함
    assert st.expr_quiz_pass == {2} and st.expr_quiz_fail == set()
    assert st.expr_judged_upto == 8


def test_the_window_marks_where_the_context_ends_and_this_span_begins() -> None:
    """⛔ 2차 반려 P2 — 겹침이 회차 **중간**에서 시작하면 창이 «복창(U)·승인(B)» 부터라 공개(B)는 창 밖이다.
    경계선이 없으면 판정기가 «(오답) 항목을 학습자가 스스로 냈고 승인받았다, 공개 없음» 으로 읽어 **앵무새가
    통과로 승격**된다. 새는 방향이 통과뿐이라(강등은 서버가 막는다) 더 위험하다.
    """
    st = _state()
    st.segments = [
        {"turn_index": 0, "role": "beaver", "text": "How do you say Please help me?"},
        {"turn_index": 1, "role": "user", "text": "음..."},
        {"turn_index": 2, "role": "beaver", "text": "It's 도와주세요. Say it."},        # 공개 — 창 밖
        {"turn_index": 3, "role": "user", "text": "도와주세요"},                        # 복창 — 겹침
        {"turn_index": 4, "role": "beaver", "text": "Great!"},                          # 승인 — 겹침
        {"turn_index": 5, "role": "beaver", "text": "Quiz time! How much is it?"},      # 이번 구간
        {"turn_index": 6, "role": "user", "text": "이거 얼마예요?"},
    ]
    st.expr_judged_upto = 5
    out, first_seen, cut = cs._expression_transcript_window(st, since=3, keep_from=5)
    assert first_seen == 3 and cut is False
    lines = out.splitlines()
    k = lines.index(cs.EXPR_WINDOW_BOUNDARY_LINE)
    assert lines[k - 1] == "선생님: Great!" and lines[k + 1].startswith("선생님: Quiz time!")
    assert "도와주세요. Say it." not in out, "시험 전제 — 공개는 창 밖이다"


def test_no_boundary_line_when_there_is_no_context() -> None:
    st = _state()
    st.segments = [{"turn_index": i, "role": "user", "text": f"발화{i}"} for i in range(3)]
    out, _, _ = cs._expression_transcript_window(st, since=0, keep_from=0)
    assert cs.EXPR_WINDOW_BOUNDARY_LINE not in out


def test_the_instruction_splits_rounds_by_where_the_decisive_moment_is() -> None:
    """⛔ 3차 반려 P1-A(codex) — «문맥 구간에서 **시작된** 회차는 판정하지 마라» 는 겹침의 목적을 스스로
    부정했다: 커서=1·seg0 질문(위)·seg1 정답(아래)이면 그 정답이 보류되고 커서 2 → **영원히 안 읽힌다.**
    회차의 결과는 정답 공개 또는 정답 산출 순간에 정해진다 — 그 순간이 어디 있느냐로 가른다.
    """
    out = _instr()
    assert f"«{cs.EXPR_WINDOW_BOUNDARY_LINE}» 줄이 있으면 그 **위는 문맥**" in out
    assert "문맥 구간(경계선 위)은 판정하지 마라 — 이미 판정됐다" in out
    assert "문맥 구간에서 (오답) 항목이 다시 보여도 새 회차가 아니다" in out
    assert "문맥 구간에서 **시작된** 회차는 판정하지 마라" not in out, "옛 규칙이 남아 있다"


def test_question_above_answer_below_is_judged() -> None:
    """codex 가 잡은 정상 답 — 질문(위)·정답(아래): 결정적 순간이 아래 → 판정 대상. 규칙 문장에 그 예외가 있다."""
    out = _instr()
    assert ("단 **그 회차의 정답 공개도 정답 산출도 문맥 구간에 없으면**(질문·힌트·오답 시도만 있으면), 이번 구간의 "
            "답을 판정해라" in out)
    # 창 모양: 질문이 경계선 위, 답이 아래 — 겹침이 이 쌍을 붙여 준다
    st = _state()
    st.segments = [
        {"turn_index": 0, "role": "beaver", "text": "Quiz! How do you say Please help me?"},
        {"turn_index": 1, "role": "user", "text": "도와주세요"},
    ]
    st.expr_judged_upto = 1
    win, first_seen, cut = cs._expression_transcript_window(st, since=0, keep_from=1)
    lines = win.splitlines()
    k = lines.index(cs.EXPR_WINDOW_BOUNDARY_LINE)
    assert lines[k - 1].startswith("선생님: Quiz!") and lines[k + 1] == "학습자: 도와주세요"


def test_question_plus_hint_above_answer_below_is_still_judged() -> None:
    """⛔ fable 3차 P2 — «질문**만** 있고» 는 너무 좁았다: 질문+힌트(위)·정답(아래)이면 «질문만» 이 아니라서
    보류 → 커서 지나감 → 그 통과 영구 누락. 힌트는 퀴즈의 정상 경로(결정 2)라 이 배치가 흔하다.
    예외는 결정적 순간 정의 그대로 — «정답 공개도 정답 산출도 문맥 구간에 없으면» 판정한다.
    """
    out = _instr()
    rule = next(l for l in out.splitlines() if "정답 공개도 정답 산출도 문맥 구간에 없으면" in l)
    assert "힌트" in rule, "예외 문장이 힌트 경로를 안 덮는다"
    assert "문맥 구간에 질문만 있고" not in out, "옛 좁은 문장이 남아 있다"


def test_reveal_above_parrot_below_is_excluded() -> None:
    """fable 이 잡은 앵무새 — 공개(위)·복창(아래): 결정적 순간이 위 → 제외."""
    out = _instr()
    assert ("문맥 구간에 이미 정답 공개나 정답 산출이 있는 회차는 이번 구간에 그 뒤 복창·반응이 보여도 다시 판정하지 마라"
            in out)


def test_the_rules_come_first_and_the_dynamic_listing_last() -> None:
    """⭐ 2차 반려 P1-1 — 목록(동적)이 두 번째 줄이면 표시가 바뀔 때마다 접두 100자부터 달라진다. 정적 접두가
    판정마다 같아야 implicit caching 이 걸릴 여지가 생긴다. 효과는 측정 몫, 손해는 없다.
    """
    plain = cs._expression_progress_instruction(ITEMS_1397, "한국어", "영어(English)")
    marked = cs._expression_progress_instruction(
        ITEMS_1397, "한국어", "영어(English)", covered_nums=[1, 2], passed_ids={2}, failed_ids={1},
    )
    head_p, tail_p = plain.rsplit("[항목 목록]", 1)
    head_m, tail_m = marked.rsplit("[항목 목록]", 1)
    assert head_p == head_m, "정적 접두가 표시 유무로 달라졌다 — 캐시가 매번 깨진다"
    assert tail_p != tail_m and "(이미 드릴함 · 통과)" in tail_m
    assert plain.rstrip().endswith("7. 네 — 뜻: yes / I see"), "목록이 맨 끝이어야 한다"
    assert "■ phase" in head_p, "규칙은 전부 목록 앞에"


def test_the_final_judge_timeout_is_two_seconds_with_its_reason_written_down() -> None:
    """2차 반려 P1-2 — 실측점 하나(6.2k=1.2s 초과)뿐이고 새 입력(≈6.4k)이 그보다 크다. 조각2 경합 최악 3초 중
    여유 1초를 남기는 2.0초. 첫 실통화의 «마지막 판정 %.0fms» 로그를 보고 다시 정한다.
    """
    assert cs.EXPR_FINAL_JUDGE_TIMEOUT_S == 2.0
    src = pathlib.Path(cs.__file__).read_text(encoding="utf-8")
    assert "6.2k 자 → 1,201ms" in src and "여유 1.0초" in src
    assert "요약 1.0~1.4초" not in src.split("EXPR_FINAL_JUDGE_TIMEOUT_S = 2.0")[0].rsplit("# ⭐⭐ **조각 끝 마지막 판정의 상한**", 1)[-1], \
        "옛 근거(요약 소요 유추)가 상수 주석에 남아 있다"


# --------------------------------------------------------------------------- #
# P1-B — 커리큘럼 표기의 대괄호(«있어요[없어요]»)는 이 통화에선 누출이 아니다
# --------------------------------------------------------------------------- #
GRAMMAR_BRACKET_ITEMS = [
    {"item_id": 101, "obj": "N이/가 있어요[없어요]", "des": "there is / isn't", "ex": None},
    {"item_id": 102, "obj": "이거는[그거는, 저거는]N이에요/예요", "des": "this/that is N", "ex": None},
]
LINE = "오늘 표현은 N이/가 있어요[없어요]입니다"


def test_curriculum_brackets_are_allowed_only_in_the_call_that_teaches_them() -> None:
    """⛔ 3차 반려 P1-B(codex) — grammar.json 459 중 21 항목이 표면형에 대괄호를 쓴다. 비버가 읽으면 한글
    대괄호 = 위치 무관 누출 → 저장 전사 훼손 + 재개 시드 주입. L2 문법을 가르치는 순간 터진다.
    허용은 **이 통화의 항목에서 온 조각만** — 다른 통화에선 같은 문장이 그대로 누출이다.
    """
    allow = cs._expression_bracket_allowlist(GRAMMAR_BRACKET_ITEMS)
    assert allow == frozenset({"[없어요]", "[그거는, 저거는]"})
    assert cs._find_control_tag_leak(LINE, allow) is None
    assert cs._scrub_control_tags(LINE, allow) == LINE
    # 그 항목이 없는 통화(일반 통화·다른 레벨)에선 여전히 누출 — 안전망은 그대로다
    assert cs._find_control_tag_leak(LINE) is not None
    assert "[없어요]" not in cs._scrub_control_tags(LINE)


def test_the_allowlist_does_not_open_the_door_for_real_tags() -> None:
    allow = cs._expression_bracket_allowlist(GRAMMAR_BRACKET_ITEMS)
    for leak in ("[안내] " + LINE, LINE + ' "[시스템]" 종료', "[통화종료:ab12] " + LINE, "[Closing] " + LINE):
        m = cs._find_control_tag_leak(leak, allow)
        assert m is not None and m.group(0) != "[없어요]", leak
        assert "[없어요]" in cs._scrub_control_tags(leak, allow) and m.group(0) not in cs._scrub_control_tags(leak, allow)


def test_the_state_carries_the_allowlist_into_the_leak_detector() -> None:
    """검출기(`_detect_tag_leak`)와 저장 정화(`_flush_beaver_segment`)가 같은 허용 목록을 쓴다."""
    st = cs._CallState()
    st.expr_items = list(GRAMMAR_BRACKET_ITEMS)
    st.expr_tag_allow = cs._expression_bracket_allowlist(st.expr_items)
    st.cur_beaver_text = [LINE]
    cs._detect_tag_leak(st)
    assert st.tag_leak_seen is False, "커리큘럼 대괄호에 재개 시드가 들어갔다"
    plain = cs._CallState()                        # 일반 통화 — 허용 목록 없음
    plain.cur_beaver_text = [LINE]
    cs._detect_tag_leak(plain)
    assert plain.tag_leak_seen is True


def test_a_fresh_state_has_an_empty_allowlist() -> None:
    assert cs._CallState().expr_tag_allow == frozenset()
    assert cs._expression_bracket_allowlist([]) == frozenset()
    assert cs._expression_bracket_allowlist([{"obj": "도와주세요"}]) == frozenset()


def test_overlap_is_dropped_before_the_must_see_region_and_is_not_a_cut() -> None:
    """⛔ 상한을 넘으면 겹침부터 버린다 — 본 구간(커서 이후)이 우선이고, 겹침이 떨어진 건 절단이 아니다
    (커서가 전진해야 한다). 반대로 커서 이후가 떨어지면 절단이다.
    """
    st = _state()
    big = "가" * 2500
    st.segments = [{"turn_index": i, "role": "user", "text": f"[{i}]" + big} for i in range(8)]
    # 커서 4, 겹침 0~3: 커서 이후 4개(≈10k)가 상한 안이라 겹침만 전부 떨어진다 — 절단 아님
    out, first_seen, cut = cs._expression_transcript_window(st, since=0, keep_from=4)
    assert first_seen == 4 and "[3]" not in out and "[4]" in out
    assert cut is False, "겹침이 떨어진 것을 절단으로 세면 커서가 영원히 멈춘다"
    # keep_from 없이(전부 꼭 봐야 함) 같은 입력이면 절단이다
    _, first_seen2, cut2 = cs._expression_transcript_window(st, since=0)
    assert first_seen2 == first_seen and cut2 is True


@pytest.mark.asyncio
async def test_a_mid_call_judgement_advances_the_cursor(monkeypatch) -> None:
    st = _state()
    st.expr_ctx = {"client": object(), "model": "m", "instruction": "i"}
    st.segments = [{"turn_index": i, "role": "user", "text": f"발화{i}"} for i in range(5)]

    async def _ok(client, model, **kw):
        return _Out(drilled=[1])

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _ok)
    await cs._expression_progress_sidecar(st)
    assert st.expr_judged_upto == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout", "none", "raise"])
async def test_a_failed_judgement_does_not_advance_the_cursor(monkeypatch, outcome: str) -> None:
    """⛔ 대입을 try 위로 올리면 미판정 구간이 **영원히 잘린다** — 아무것도 안 깨지고 조용히 사라진다.
    timeout·None·예외 어느 쪽이든 upto 는 그대로여야 다음 판정이 그 구간을 다시 본다.
    """
    st = _state()
    st.expr_ctx = {"client": object(), "model": "m", "instruction": "i"}
    st.segments = [{"turn_index": i, "role": "user", "text": f"발화{i}"} for i in range(6)]
    st.expr_judged_upto = 2
    st.expr_phase = "quiz"

    async def _fail(client, model, **kw):
        if outcome == "timeout":
            await asyncio.sleep(5)
        if outcome == "raise":
            raise RuntimeError("boom")
        return None

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _fail)
    monkeypatch.setattr(cs, "EXPR_FINAL_JUDGE_TIMEOUT_S", 0.05)
    await cs._final_expression_progress(st)
    assert st.expr_judged_upto == 2, f"{outcome}: 판정이 안 됐는데 커서가 움직였다"
    assert st.expr_phase == "quiz", f"{outcome}: 판정이 안 됐는데 구간이 바뀌었다"


@pytest.mark.asyncio
async def test_a_cut_input_limits_the_loss_to_once_and_logs_it(monkeypatch, caplog) -> None:
    """⛔ 1차 반려 P2-A(codex) 는 «잘렸으면 커서 불전진» 이었다. 2차 반려(fable)가 그 잠복을 짚었다:
    커서 0 에서 절단 → 다음도 since 0 → 창은 **앞에서만 커지므로** 잘린 머리는 어디에도 다시 들어가지 않고,
    그 뒤 모든 판정이 12k 최대 입력 → 마지막 판정 확정 타임아웃. «다음 판정에 다시 들어간다» 는 거짓이었다.
    ⇒ 커서는 «본 끝» 까지 간다 — 손실은 [커서, first_seen) **그 한 번**이고 경고 로그에 세그먼트 번호로 남는다.
    ⚠ 도달성: 1397 = 316초에 3.4k ⇒ 12k ≈ 16분 — 5분 소켓에선 못 채운다(잠복). 싸서 고쳤다.
    """
    st = _state()
    st.expr_ctx = {"client": object(), "model": "m", "instruction": "i"}
    st.segments = [{"turn_index": 0, "role": "user", "text": "SENTINEL"}] + [
        {"turn_index": i, "role": "user", "text": f"[{i:02d}]" + "가" * 496} for i in range(1, 31)
    ]
    seen: dict = {}

    async def _capture(client, model, **kw):
        seen["prompt"] = kw.get("prompt", "")
        return _Out(drilled=[1], phase="drill")

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _capture)
    with caplog.at_level("WARNING", logger=cs.logger.name):
        await cs._expression_progress_sidecar(st)
    assert "SENTINEL" not in seen["prompt"], "시험 전제 — 머리가 잘려야 한다"
    first_seen = next(i for i in range(1, 31) if f"[{i:02d}]" in seen["prompt"])
    assert st.expr_judged_upto == 31, "커서는 «본 끝» 까지 — 손실을 한 번으로 한정한다"
    assert cs._expr_covered_ids(st) == [1]
    assert any(f"세그먼트 0~{first_seen - 1} 미판정" in r.getMessage() for r in caplog.records), \
        "잘린 구간이 세그먼트 번호로 로그에 남아야 한다"
    # 다음 판정은 «본 끝» 에서(겹침만큼 앞에서) 시작한다 — 다시 12k 가 아니다
    st.segments += [{"turn_index": 31, "role": "user", "text": "다음"}]
    await cs._expression_progress_sidecar(st)
    assert "SENTINEL" not in seen["prompt"] and "[26]" not in seen["prompt"]
    assert "[27]" in seen["prompt"] and "다음" in seen["prompt"] and st.expr_judged_upto == 32


@pytest.mark.asyncio
async def test_a_late_old_judgement_does_not_roll_the_phase_back(monkeypatch) -> None:
    """⛔ 반려 P2-B(codex) — 오래 걸린 snapshot@10(quiz) 이 최신 snapshot@20(drill) **뒤에** 도착하면
    cursor=20 인데 phase=quiz 로 역전 → 다음 꼬리 머리에 거짓 «현재 구간: 퀴즈». LLM 오판 없이
    유효한 판정 둘만으로 오염된다. phase 는 커서와 **같은 세대**로만 갱신한다(max-update).
    """
    st = _state()
    st.expr_ctx = {"client": object(), "model": "m", "instruction": "i"}
    st.segments = [{"turn_index": i, "role": "user", "text": f"발화{i}"} for i in range(10)]
    gate = asyncio.Event()

    async def _slow_then_fast(client, model, **kw):
        if "발화19" not in kw.get("prompt", ""):     # 옛 판정(@10) — 새 판정이 끝날 때까지 기다린다
            await gate.wait()
            return _Out(phase="quiz")
        return _Out(phase="drill")                     # 새 판정(@20)

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _slow_then_fast)
    old = asyncio.create_task(cs._expression_progress_sidecar(st))
    await asyncio.sleep(0)                             # 옛 판정이 전사를 잡고 LLM 대기에 들어간다
    st.segments += [{"turn_index": i, "role": "user", "text": f"발화{i}"} for i in range(10, 20)]
    await cs._expression_progress_sidecar(st)          # 새 판정이 먼저 끝난다
    assert st.expr_judged_upto == 20 and st.expr_phase == "drill"
    gate.set()
    await old                                          # 옛 판정이 늦게 도착
    assert st.expr_judged_upto == 20
    assert st.expr_phase == "drill", "늦게 온 옛 판정이 phase 를 되돌렸다"
    assert st.expr_phase_upto == 20


@pytest.mark.asyncio
async def test_with_no_mid_call_judgement_the_final_one_reads_the_whole_transcript(monkeypatch) -> None:
    """통화중 판정 0회(짧은 통화·arm 미도달)면 마지막 판정이 **전사 전체**를 본다 — since=0.
    ⚠ 엄밀히는 «12k 상한 안에서 전체» 다 — 넘으면 앞이 잘리고 커서는 안 움직인다(위 시험).
    """
    st = _state()
    st.expr_ctx = {"client": object(), "model": "m", "instruction": "i"}
    st.segments = [{"turn_index": i, "role": "user", "text": f"발화{i}"} for i in range(5)]
    assert st.expr_judged_upto == 0

    seen: dict = {}

    async def _capture(client, model, **kw):
        seen["prompt"] = kw.get("prompt", "")
        return _Out(phase="drill")

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _capture)
    await cs._final_expression_progress(st)
    assert all(f"발화{i}" in seen["prompt"] for i in range(5))
    assert "[앞 구간 생략" not in seen["prompt"], "안 잘랐는데 머리가 붙었다"


# --------------------------------------------------------------------------- #
# C2 — 절단·커서 시작 시 «현재 구간» 을 머리에 보존
# --------------------------------------------------------------------------- #
def test_the_phase_is_preserved_when_the_head_is_cut() -> None:
    """⛔ 뒤에서 자르면 퀴즈 앵커 문장이 잘리고 **답만 남을 수 있다** — 판정기가 퀴즈 답을
    드릴로 읽어 통과가 사라진다. 직전 판정이 본 구간을 머리에 한 줄로 남긴다.
    """
    st = _state()
    st.expr_phase = "quiz"
    st.segments = [{"turn_index": i, "role": "user", "text": "가" * 500} for i in range(60)]
    out = cs._expression_transcript(st)
    assert out.startswith("[앞 구간 생략 — 직전 판정 기준 현재 구간: 퀴즈]")


def test_the_phase_is_preserved_when_starting_from_a_cursor() -> None:
    st = _state()
    st.expr_phase = "drill"
    st.segments = [{"turn_index": i, "role": "user", "text": f"발화{i}"} for i in range(4)]
    out = cs._expression_transcript(st, since=2)
    assert out.startswith("[앞 구간 생략 — 직전 판정 기준 현재 구간: 드릴]")


def test_no_phase_header_when_nothing_was_cut() -> None:
    """⚠ 앵커가 온전히 들어 있으면 머리를 붙이지 않는다 — 판정기에 잡음을 주지 않는다."""
    st = _state()
    st.expr_phase = "quiz"
    st.segments = [{"turn_index": 0, "role": "beaver", "text": "Quiz time!"}]
    assert not cs._expression_transcript(st).startswith("[앞 구간 생략")


@pytest.mark.asyncio
async def test_the_sidecar_remembers_the_last_phase(monkeypatch) -> None:
    st = _state()
    st.expr_ctx = {"client": object(), "model": "m", "instruction": "i"}
    st.segments = [{"turn_index": 0, "role": "user", "text": "음"}]

    async def _ok(client, model, **kw):
        return _Out(phase="quiz")

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _ok)
    await cs._expression_progress_sidecar(st)
    assert st.expr_phase == "quiz"


@pytest.mark.asyncio
async def test_an_ambiguous_phase_clears_the_remembered_one(monkeypatch) -> None:
    """⛔ 반려 P2-1 — 모호("")인데 지난 값을 남기면 두 판정 전 구간이 다음 입력 머리에
    «직전 판정 기준» 이라고 **거짓으로** 붙는다. 거짓 머리보다 머리 없음이 낫다.
    """
    st = _state()
    st.expr_ctx = {"client": object(), "model": "m", "instruction": "i"}
    st.expr_phase = "quiz"
    st.segments = [{"turn_index": 0, "role": "user", "text": "음"}]

    async def _vague(client, model, **kw):
        return _Out(phase="")

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _vague)
    await cs._expression_progress_sidecar(st)
    assert st.expr_phase == ""
    assert not cs._expression_transcript(st, since=1).startswith("[앞 구간 생략")


# --------------------------------------------------------------------------- #
# C4 — 자리표시 [Country] 는 누출이 아니다 · 진짜 누출은 계속 잡힌다
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ok", [
    "How do you say 'I'm from [Country]'?", "Say hi to [Name]!", "저는 [Place] 가요", "I'm [Your Name]",
])
def test_english_placeholders_inside_a_sentence_are_not_control_tag_leaks(ok: str) -> None:
    """⛔ 1397 33:37 — «[Country]» 를 누출로 잡아 정상 문장을 자르고 복구 시드를 넣었다.
    자리표시는 문장 **안**에 온다 — 어휘를 미리 알 필요 없이 위치로 가른다(반려 P2-2).
    """
    assert cs._find_control_tag_leak(ok) is None
    assert cs._scrub_control_tags(ok) == ok


@pytest.mark.parametrize("leak", ["[Closing] Bye!", '"[Closing]" Bye', "[Note] see you", "[안내] 네"])
def test_an_invented_tag_at_the_head_is_a_leak_even_if_it_looks_like_a_placeholder(leak: str) -> None:
    """⛔ 반려 P2-2 — call 870 «[마무리] 네. 수고하셨어요» ×8 은 **없는 태그 발명**이었다. 표현학습은
    비버가 영어로 말하니 같은 발명이 [Closing]·[Note] 꼴로 나온다 — 모양만 보면 자리표시와 겹친다.
    발명 태그는 발화 **맨 앞**에 온다(따옴표째 인용해도 맨 앞이다). 맨 앞 대괄호는 무조건 누출.
    """
    assert cs._find_control_tag_leak(leak) is not None
    assert "[" not in cs._scrub_control_tags(leak)


@pytest.mark.parametrize("leak", [
    "[공부 모드] 시작", '"[시스템]" 통화가', "[통화종료:ab12] 안녕", "[Quiz Time] go", "네. 다음은 [시스템] 종료",
])
def test_real_control_tags_are_still_caught_anywhere(leak: str) -> None:
    """⚠ 한글·콜론·숫자를 품은 대괄호는 **어디에 있어도** 걸린다 — 자기낭독 안전망(call 706) 유지."""
    assert cs._find_control_tag_leak(leak) is not None
    assert "[" not in cs._scrub_control_tags(leak)


def test_a_placeholder_does_not_hide_a_real_leak_behind_it() -> None:
    """⚠ `search` 를 그대로 썼다면 첫 대괄호(자리표시)에서 멈춰 뒤의 진짜 누출을 놓친다."""
    m = cs._find_control_tag_leak("Say [Country] 라고 해봐. [시스템] 종료")
    assert m is not None and m.group(0) == "[시스템]"


# --------------------------------------------------------------------------- #
# B. 비버 대본 — 7줄
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "line",
    [
        # ① 퀴즈 앵커
        "퀴즈를 시작한다는 말을 영어(English)로 먼저 해라",
        "퀴즈를 마치고 새 표현으로 돌아갈 때도 영어(English)로 알려라",
        # ② 먼저 묻고 기다려라
        "물은 뒤 **반드시 기다려라.**",
        "**다음 항목은 다시 먼저 물어라** — 답을 먼저 주지 마라",
        # ④ 공개 = 그 회차의 끝
        "**표현 전체나 그 어절을 말하지 마라.**",
        "그 순간 그 문항은 틀린 것이고, 직후에 따라 말해도 바뀌지 않는다",
        # ⑤ 격식
        "반말로 답하면 맞힌 게 아니다",
        "격식 표지(-요·-습니다·저)가 빠진 것은 아니다",
        # ⑥ 무음
        "학습자가 조용하면 오답으로 치지 마라",
        "두 번째 연속 무음이면 들려주고 따라 말하게 해라",
    ],
)
def test_the_beaver_script_carries_each_t14_line(line: str) -> None:
    assert line in _script(), f"T14 대본 줄이 빠졌다: {line}"


def test_the_old_zero_output_rule_is_gone() -> None:
    """②가 갈아 끼운 자리 — «최악의 경우 따라 말하기로라도» 가 남아 있으면 답을 먼저 준다."""
    assert "최악의 경우 따라 말하기로라도" not in _script()


def test_the_note_tells_the_beaver_to_ask_first_on_a_new_item() -> None:
    """③ — 쪽지 직후 비버가 정답을 먼저 말하는 경로를 막는다."""
    note = build_expression_reground_brief("r", "p", drilled=["가"], locale_label="영어(English)")
    assert "**새 항목은** 먼저 영어(English)로 묻고 기다려라. 답을 먼저 말하지 마라." in note
    assert "학습자가 방금 답했으면 그 답에 먼저 반응해라" in note


def test_the_nudge_gives_a_hint_in_a_quiz_and_a_model_in_a_drill() -> None:
    """⑦ — 퀴즈 중 넛지가 정답을 들려주면 그 문항이 통째로 죽는다."""
    assert "퀴즈 중이면 정답 대신 힌트 하나만 주고" in NUDGE_SEED_1_EXPRESSION
    assert "드릴 중이면 한 번 더 들려준 뒤" in NUDGE_SEED_1_EXPRESSION


# --------------------------------------------------------------------------- #
# T15 — 통화 1398 감사 반영 (퀴즈 주기 · 포기 경로 · 공개 뒤 되묻기 · 표시 스냅샷 · arm 라벨/바닥)
# --------------------------------------------------------------------------- #
def test_the_quiz_section_says_when_once_and_how_below_it() -> None:
    """T15-1·2 — 1398 에서 퀴즈가 매 항목 직후(t13·t23·t29) 나왔다. «어떻게» 5줄이 «언제» 1줄을 덮었다."""
    out = _script()
    quiz = out.split("[퀴즈]", 1)[1].split("[반응", 1)[0]
    assert quiz.index("- 언제:") < quiz.index("- 어떻게:")
    assert "그 사이(묶음이 아직 안 찼을 때)에는 퀴즈·복습·테스트를 **절대 시작하지 마라**" in quiz
    assert "방금 끝낸 항목 하나를 '퀴즈' 라며 되묻는 것은 퀴즈가 아니라 드릴 재시도다" in quiz
    assert "정답은 그 묶음에서 배운 표현이다" in quiz and "방금 다룬 그 표현 자체" not in out
    assert "그 외엔 항상 3개 묶음이다" in quiz
    # T14 확정 문장은 뜻 그대로 «어떻게» 아래에 있다(배치만 바뀜)
    for kept in ("퀴즈를 시작한다는 말을 영어(English)로 먼저 해라", "**표현 전체나 그 어절을 말하지 마라.**",
                 "직후에 따라 말해도 바뀌지 않는다", "이 통화 안에서 뒤에 한 번 더 낸다"):
        assert kept in quiz, kept
    assert "[진행 절차]" in out and out.index("[진행 절차]") < out.index("[퀴즈]")


def test_the_give_up_path_never_says_correct() -> None:
    """T15-3 — 1398 t9: 3번째 시도 「잘 못 들었다」(반말·오답)에 극찬. 포기 경로에 «맞았다고 하지 마라» 가 없었다."""
    out = _script()
    assert "다음 번호 항목으로 이어 가라 — **맞았다고 하지는 마라.** 캐릭터대로 넘기되 틀린 건 틀린 거다" in out
    assert "항목 하나에 집착 금지 — 그리고 맞았다고 하지는 마라" in out


def test_no_question_form_reask_after_a_reveal_on_both_sides() -> None:
    """T15-5 — 1398 t17 «The word is 안녕하세요. Now tell me, how do you say Hello?» → 판정기가 새 문항으로 읽음."""
    assert "정답을 들려준 뒤에는 '어떻게 말해요?' 로 되묻지 마라 — 따라 말하게만 해라" in _script()
    ins = _instr()
    assert "정답 공개와 같은 턴이나 바로 다음 턴의 되묻기는 **질문형이어도**(«어떻게 말해요?») 같은 회차다" in ins
    assert "새 회차는 재출제 규칙뿐이다" in ins


def test_marks_come_from_the_last_judgement_snapshot_not_live_detection() -> None:
    """T15-4 — 1398 항목 6(t39-43): 표시가 «지금까지 검출» 이라 창 안 첫 드릴이 이미 드릴함으로 실려 판정기가
    퀴즈로 읽고 오답을 찍었다. 표시는 **직전 판정 완료 시점 스냅샷**에서 만든다."""
    st = _state()
    st.expr_ctx = {"client": object(), "model": "m", "target_language": "한국어", "locale_label": "영어(English)"}
    st.expr_covered_snapshot = [1, 2, 3]       # 직전 판정 전에 드릴된 것
    st.covered_nums = [1, 2, 3, 4]             # 4 는 이번 창 안에서 문자열 검출로 막 들어온 첫 드릴
    ins = cs._expression_judge_instruction(st)
    assert "3. 저는 ◯◯ 사람이에요 — 뜻: I'm from ◯◯  (이미 드릴함)" in ins
    assert "4. 도와주세요 — 뜻: Please help me  (" not in ins, "창 안 첫 드릴 항목에 표시가 붙었다"


@pytest.mark.asyncio
async def test_the_snapshot_is_capture_time_covered_plus_this_judgements_drilled(monkeypatch) -> None:
    """스냅샷 = 입력을 잡은 순간의 covered + 이번 판정의 drilled. 판정 도는 동안 검출된 항목은 섞이지 않는다."""
    st = _state()
    st.expr_ctx = {"client": object(), "model": "m", "instruction": "i"}
    st.covered_nums = [1]
    st.segments = [{"turn_index": 0, "role": "user", "text": "음"}]

    async def _slow(client, model, **kw):
        st.covered_nums.append(5)              # LLM 대기 중 다음 창의 첫 드릴이 검출됐다
        return _Out(drilled=[1, 2], phase="drill")

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _slow)
    await cs._expression_progress_sidecar(st)
    assert st.expr_covered_snapshot == [1, 2], "판정 중 검출된 5 가 스냅샷에 섞였다"
    assert st.covered_nums == [1, 5, 2]       # covered_nums 자체는 실시간 합집합 그대로
    assert st.expr_snapshot_upto == 1


@pytest.mark.asyncio
async def test_a_late_old_judgement_does_not_roll_the_snapshot_back(monkeypatch) -> None:
    st = _state()
    st.expr_ctx = {"client": object(), "model": "m", "instruction": "i"}
    st.segments = [{"turn_index": i, "role": "user", "text": f"발화{i}"} for i in range(10)]
    gate = asyncio.Event()

    async def _slow_then_fast(client, model, **kw):
        if "발화19" not in kw.get("prompt", ""):
            await gate.wait()
            return _Out(drilled=[1])
        return _Out(drilled=[1, 2, 3])

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _slow_then_fast)
    old = asyncio.create_task(cs._expression_progress_sidecar(st))
    await asyncio.sleep(0)
    st.segments += [{"turn_index": i, "role": "user", "text": f"발화{i}"} for i in range(10, 20)]
    await cs._expression_progress_sidecar(st)
    assert st.expr_covered_snapshot == [1, 2, 3]
    gate.set()
    await old
    assert st.expr_covered_snapshot == [1, 2, 3], "늦게 온 옛 판정이 스냅샷을 되돌렸다"


def test_a_fresh_state_has_no_marks_snapshot() -> None:
    st = cs._CallState()
    assert st.expr_covered_snapshot == [] and st.expr_snapshot_upto == 0


def test_expression_pre_arm_is_off_when_the_room_is_too_narrow_but_normal_is_unchanged() -> None:
    """T15-6 — 1398: 첫 arm 이 40초에 «근거=compress», 실제 압축은 3분 22초 뒤. T14 지시문이 바닥을 트리거에
    붙여 room×0.85 가 대화 몇 턴 분량이 됐다. 표현학습은 room 이 하한보다 좁으면 ①을 끈다. 일반 통화는 그대로."""
    trigger = cs._settings.LIVE_CTX_TRIGGER_TOKENS
    floor = trigger - (cs.EXPR_REGROUND_MIN_ROOM_TOKENS - 500)     # room = 하한 − 500
    for is_expr in (True, False):
        st = cs._CallState()
        st.call_start_ts = 1000.0
        if is_expr:
            st.expr_items = list(ITEMS_1397)
        cs._observe_compression(st, floor)
        st.usage_prompt_peak = trigger                                # 산식만 보면 임박
        got = cs._reground_due(st, 1001.0)
        if is_expr:
            assert got == "", "표현학습: 좁은 room 에서 임박 산식이 발동했다(1398 40초 arm 재현)"
        else:
            assert got == "compress_imminent", "일반 통화 동작이 바뀌었다"
    # room 이 넉넉하면 표현학습도 임박 산식이 산다
    st = cs._CallState()
    st.call_start_ts = 1000.0
    st.expr_items = list(ITEMS_1397)
    wide_floor = trigger - (cs.EXPR_REGROUND_MIN_ROOM_TOKENS + 2000)
    cs._observe_compression(st, wide_floor)
    st.usage_prompt_peak = trigger
    assert cs._reground_due(st, 1001.0) == "compress_imminent"


def test_the_imminence_label_is_distinct_from_actual_compression() -> None:
    """계측 정직성 — «compress» 한 단어가 산식 발동과 실제 감지를 섞어 1398 감사를 헷갈리게 했다."""
    src = pathlib.Path(cs.__file__).read_text(encoding="utf-8")
    body = src.split("def _reground_due(", 1)[1].split("\ndef ", 1)[0]
    assert 'return "compress_imminent"' in body and 'return "post-compress"' in body
    assert 'return "compress"' not in body


def test_the_beaver_script_still_has_no_example_lines() -> None:
    """⛔ 판정기(A)에는 영어 예시가 있어도 되지만 **비버 대본에는 없다** — 리터럴이 대사로 샌다."""
    out = _script()
    for literal in ("quiz time", "let's review", "let's see if you remember"):
        assert literal not in out.lower(), f"판정기용 예시가 대본으로 새어 들어갔다: {literal}"
