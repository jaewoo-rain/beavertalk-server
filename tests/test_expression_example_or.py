"""판정 보강(2026-09-12, bt-back) — 표현학습 대조·판정에 **예문 OR**(`quiz_judge.item_mentioned`).

문법 항목의 표면형은 주형(「V-아요/어요」)이라 발화에 그대로 안 나오고, 비버·학습자는 예문(「나무가 타요.」)을 말한다.
covered(`_note_covered_items`)·판정(`_server_judge_quiz`) 두 곳이 예문까지 본다. STT 폴백 검증(`_verify_stt_fallback`)도 앞·뒤 B 의 예문 공개를 본다(bt-back 결정 1). normal 통화는 무변경.
"""
from __future__ import annotations

import domains.learning.realtime.call_session as cs
from domains.learning.service import quiz_judge

GRAMMAR = {"item_id": 31, "obj": "V-아요/어요", "des": "present polite ending", "ex": "나무가 타요."}
WATER = {"item_id": 32, "obj": "물", "des": "water", "ex": "물이 있어요."}
YES = {"item_id": 33, "obj": "네", "des": "yes", "ex": "네."}
ITEMS = [GRAMMAR, WATER, YES]


def _state() -> cs._CallState:
    st = cs._CallState()
    st.expr_items = list(ITEMS)
    st.reground_items = [i["obj"] for i in ITEMS]
    return st


# --------------------------------------------------------------------------- #
# 순수 함수
# --------------------------------------------------------------------------- #
def test_item_mentioned_accepts_the_example_sentence_for_a_grammar_template() -> None:
    assert quiz_judge.mentions("나무가 타요.", "V-아요/어요") is False, "표면형만으로는 안 잡힌다(전제)"
    assert quiz_judge.item_mentioned("나무가 타요.", "V-아요/어요", "나무가 타요.") is True
    assert quiz_judge.item_mentioned("Repeat after me: 나무가 타요", "V-아요/어요", "나무가 타요.") is True
    assert quiz_judge.item_mentioned("저는 학생이에요", "V-아요/어요", "나무가 타요.") is False


def test_item_mentioned_falls_back_to_mentions_when_no_example() -> None:
    for text in ("물이 있어요", "어제 선물을 받았어요", "물"):
        assert quiz_judge.item_mentioned(text, "물", None) is quiz_judge.mentions(text, "물")
        assert quiz_judge.item_mentioned(text, "물", "") is quiz_judge.mentions(text, "물")


def test_item_mentioned_ignores_examples_shorter_than_4_chars() -> None:
    """「네.」「가요.」 는 어디서나 나온다 — 예문 OR 를 걸지 않는다(표면형 경로는 그대로)."""
    assert quiz_judge.item_mentioned("네, 알겠어요", "물", "네.") is False
    assert quiz_judge.item_mentioned("네, 알겠어요", "네", "네.") is True          # 표면형 「네」 어절 일치


def test_item_mentioned_keeps_the_sunmul_regression_closed() -> None:
    """「물」 ← "어제 선물을 받았어요" 는 여전히 거짓 — 예문 「물이 있어요.」 도 그 발화에 없다."""
    assert quiz_judge.item_mentioned("어제 선물을 받았어요", "물", "물이 있어요.") is False
    assert quiz_judge.item_mentioned("물이 있어요", "물", "물이 있어요.") is True


# --------------------------------------------------------------------------- #
# covered
# --------------------------------------------------------------------------- #
def test_covered_counts_a_grammar_item_when_the_beaver_quotes_its_example() -> None:
    st = _state()
    cs._note_covered_items(st, "Repeat after me: 나무가 타요.", source="beaver")
    assert st.covered_nums == [1]


def test_covered_counts_the_example_from_the_learner_too_in_expression() -> None:
    st = _state()
    cs._note_covered_items(st, "나무가 타요", source="user")
    assert st.covered_nums == [1]


def test_covered_does_not_fire_on_an_unrelated_polite_sentence_via_example() -> None:
    st = _state()
    cs._note_covered_items(st, "저는 학생이에요", source="beaver")
    assert 1 not in st.covered_nums


def test_item_example_is_none_when_numbering_is_misaligned() -> None:
    st = _state()
    assert cs._item_example(st, 1) == "나무가 타요."
    assert cs._item_example(st, 1, "V-아요/어요") == "나무가 타요."
    assert cs._item_example(st, 1, "다른 표면형") is None
    assert cs._item_example(st, 99) is None


# --------------------------------------------------------------------------- #
# 판정
# --------------------------------------------------------------------------- #
def _judge(lines: list[tuple[str, str]], quiz_set=(1,)):
    st = _state()
    span = [(i, "beaver" if r == "B" else "user", t) for i, (r, t) in enumerate(lines)]
    res = cs._server_judge_quiz(st, span, list(quiz_set))
    return st, res


def test_learner_saying_the_example_first_is_passed() -> None:
    st, res = _judge([("B", "How do you say 'the tree is burning'?"), ("U", "나무가 타요."), ("B", "Great!")])
    assert res["passed"] == [1] and st.expr_quiz_pass == {31}


def test_beaver_revealing_the_example_first_is_failed() -> None:
    st, res = _judge([("B", "How do you say it?"), ("U", "음..."), ("B", "It's 나무가 타요."), ("U", "나무가 타요")])
    assert res["failed"] == [1] and st.expr_quiz_fail == {31} and st.expr_quiz_pass == set()


def test_an_informal_example_answer_is_not_an_event() -> None:
    """표면형이 «요» 로 끝나므로 격식 검사는 그대로 — 「나무가 타」 는 사건이 아니다(미판정)."""
    st, res = _judge([("B", "How do you say it?"), ("U", "나무가 타"), ("B", "Hmm.")])
    assert res["pending"] == [1] and st.expr_quiz_pass == set() and st.expr_quiz_fail == set()


def test_no_mention_stays_pending() -> None:
    st, res = _judge([("B", "How do you say it?"), ("U", "저는 학생이에요")])
    assert res["pending"] == [1]


# --------------------------------------------------------------------------- #
# normal 무변경
# --------------------------------------------------------------------------- #
def test_normal_call_covered_detection_is_byte_for_byte_the_old_raw_substring() -> None:
    st = cs._CallState()
    st.expr_items = []                                  # normal — 게이트
    st.reground_items = ["물", "V-아요/어요"]
    cs._note_covered_items(st, "어제 선물을 받았어요", source="beaver")
    assert st.covered_nums == [1], "normal 은 생짜 `label in text` 그대로(예문 OR·낱말 경계 둘 다 안 탄다)"
    cs._note_covered_items(st, "나무가 타요", source="user")
    assert st.covered_nums == [1], "normal 은 학습자 발화를 보지 않는다"


# --------------------------------------------------------------------------- #
# STT 폴백 검증 — 비버가 **예문**을 말한 것도 공개·정정이다(bt-back 결정 1)
# --------------------------------------------------------------------------- #
def test_stt_fallback_verification_rejects_a_prior_or_next_beaver_example_reveal() -> None:
    st = _state()
    st.expr_quiz_open_seg = 0
    # 앞 B 가 예문을 말했다 → 공개
    span = [(0, "beaver", "It's 나무가 타요. Say it."), (1, "user", "나무가 타요")]
    assert cs._verify_stt_fallback(st, span, 1, 1) == (False, "앞 B 에 공개")
    # 다음 B 가 예문으로 정정했다 → 정정
    span = [(0, "beaver", "How do you say it?"), (1, "user", "나무가 타요"), (2, "beaver", "Almost — 나무가 타요.")]
    assert cs._verify_stt_fallback(st, span, 1, 1) == (False, "다음 B 가 정정")
    # 앞뒤 B 에 표면형·예문 둘 다 없으면 통과(격식 «요» 충족)
    span = [(0, "beaver", "How do you say it?"), (1, "user", "나무가 타요"), (2, "beaver", "Great!")]
    assert cs._verify_stt_fallback(st, span, 1, 1) == (True, "")
