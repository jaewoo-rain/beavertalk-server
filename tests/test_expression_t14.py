# -*- coding: utf-8 -*-
"""T14·T15 잔존 회귀 — 비버 대본 7줄 · 제어태그 자리표시/커리큘럼 대괄호 · arm 라벨/바닥 · 1397 픽스처.

## 왜 — 첫 실통화(1397, 사장님 316초)에서 판정이 네 겹으로 틀렸다
    ① 앵무새를 통과로 셈          ② 드릴 산출을 퀴즈 통과로 셈(첫 퀴즈 전에 통과)
    ③ failed 구조적으로 0         ④ 마지막 판정 1,201ms > 상한 → 마지막 145초 미판정
⇒ 사장님 재정의: «배웠는가 / 맞췄는가».

## ⚠ T16(2026-09-11) 에서 지운 것
옛 LLM 판정기(지시문 규칙 문장 · 커서/겹침/경계선 · phase · 표시 스냅샷)와 그 시험 42개는 **지웠다** — 퀴즈를
서버가 열고 passed/failed 를 서버가 찾는다(tests/test_expression_t16.py). 여기 남은 것은 그 설계와 무관하게
살아 있는 규칙들이다. 1397 전사 픽스처는 재측정 기준선(drilled={1..7} · passed={6} · failed={1,2,3,4,5})으로 남긴다.
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
def test_the_give_up_path_never_says_correct() -> None:
    """T15-3 — 1398 t9: 3번째 시도 「잘 못 들었다」(반말·오답)에 극찬. 포기 경로에 «맞았다고 하지 마라» 가 없었다."""
    out = _script()
    assert "다음 번호 항목으로 이어 가라 — **맞았다고 하지는 마라.** 캐릭터대로 넘기되 틀린 건 틀린 거다" in out
    assert "항목 하나에 집착 금지 — 그리고 맞았다고 하지는 마라" in out


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
