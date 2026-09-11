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
1397 전사는 픽스처로 박아 **기대 판정**(drilled={1..7} · passed={6} · failed={1,2,3,4,5})을
문서화한다 — 실통화 재측정 때 같은 전사로 비교할 기준선이다.
"""

from __future__ import annotations

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


def test_the_1397_expected_verdict_applies_cleanly() -> None:
    """기대 판정을 그대로 얹으면 서버 상태가 그 모양이 된다 — 실통화 비교의 목표 상태다."""
    st = _state()
    e = EXPECTED_1397
    cs._apply_expression_progress(
        st, _Out(drilled=sorted(e["drilled"]), passed=sorted(e["passed"]),
                 failed=sorted(e["failed"]), phase="drill"),
    )
    assert set(cs._expr_covered_ids(st)) == e["drilled"]
    assert st.expr_quiz_pass == e["passed"]
    assert st.expr_quiz_fail == e["failed"]


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
async def test_the_final_judgement_only_sends_the_unjudged_tail(monkeypatch) -> None:
    """⛔ 전사 전체를 넣으면 새 지시문(옛것의 2배) 아래서 1397 처럼 상한을 넘긴다
    (1,201ms → 마지막 145초 미판정). 앞 구간 결과는 state 에 합집합으로 이미 있다.
    """
    st = _state()
    st.expr_ctx = {"client": object(), "model": "m", "instruction": "i"}
    st.segments = [{"turn_index": i, "role": "user", "text": f"발화{i}"} for i in range(10)]
    st.expr_judged_upto = 7                            # 통화 중 판정이 7개까지 봤다

    seen: dict = {}

    async def _capture(client, model, **kw):
        seen["prompt"] = kw.get("prompt", "")
        return _Out(phase="quiz")

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _capture)
    await cs._final_expression_progress(st)
    assert "발화7" in seen["prompt"] and "발화9" in seen["prompt"]
    assert "발화6" not in seen["prompt"], "이미 판정한 구간이 다시 들어갔다"


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


# --------------------------------------------------------------------------- #
# C4 — 자리표시 [Country] 는 누출이 아니다 · 진짜 누출은 계속 잡힌다
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ok", ["How do you say 'I'm from [Country]'?", "[Name]!", "[Place] 가요"])
def test_english_placeholders_are_not_control_tag_leaks(ok: str) -> None:
    """⛔ 1397 33:37 — «[Country]» 를 누출로 잡아 정상 문장을 자르고 복구 시드를 넣었다."""
    assert cs._find_control_tag_leak(ok) is None
    assert cs._scrub_control_tags(ok) == ok


@pytest.mark.parametrize("leak", ["[공부 모드] 시작", '"[시스템]" 통화가', "[통화종료:ab12] 안녕", "[Quiz Time] go"])
def test_real_control_tags_are_still_caught(leak: str) -> None:
    """⚠ 한글·콜론·숫자·두 단어는 자리표시 패턴에 안 맞아 그대로 걸린다 — 자기낭독 안전망 유지."""
    assert cs._find_control_tag_leak(leak) is not None
    assert "[" not in cs._scrub_control_tags(leak)


def test_a_placeholder_does_not_hide_a_real_leak_behind_it() -> None:
    """⚠ `search` 를 그대로 썼다면 첫 대괄호(자리표시)에서 멈춰 뒤의 진짜 누출을 놓친다."""
    m = cs._find_control_tag_leak("[Country] 라고 해봐. [시스템] 종료")
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


def test_the_beaver_script_still_has_no_example_lines() -> None:
    """⛔ 판정기(A)에는 영어 예시가 있어도 되지만 **비버 대본에는 없다** — 리터럴이 대사로 샌다."""
    out = _script()
    for literal in ("quiz time", "let's review", "let's see if you remember"):
        assert literal not in out.lower(), f"판정기용 예시가 대본으로 새어 들어갔다: {literal}"
