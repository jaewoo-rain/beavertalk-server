# -*- coding: utf-8 -*-
"""T16 — 퀴즈를 서버가 연다: 상태기계 · 서버 판정 · STT 폴백 검증 · 템플릿 대조 (2026-09-11, 외부 의존 0).

설계문: docs/20260911_2000_표현학습-T16-서버주도-퀴즈-상태기계.md
## 왜
판정기 지시문에 «드릴 정답은 passed 가 아니다» 가 명시돼 있는데도 4통화 일관해서 드릴 첫 시도 정답이 passed 로
찍혔다(1404: 7/18). 비버 대본에 «3·6·9번 직후에만» 이 있는데도 3/4 통화가 6·7·10번째에 냈다. 둘 다 **LLM 이
세거나 구간을 가르는 일**이고 세 번 고쳐도 남았다 ⇒ 세는 것·구간 여는 것·passed/failed 찾는 것을 **서버**가 한다.
LLM 은 서버가 글자로 못 찾은 항목의 STT 폴백만.

## ⛔ 이 파일이 재는 것
상태기계(큐 arm·얹기·열림·닫힘·꼬리·오답 재출제·종료) · 서버 판정(첫 사건 규칙·단조) · 폴백 3조건 · 템플릿 대조 ·
큐가 재접지 카운터/간격을 안 건드림 · 복창이 창 첫 U 인 경로 → 미통과 · J 혼동 → 안 닫힘.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib

import pytest

import domains.learning.realtime.call_session as cs
from core.prompts.common import CONTROL_TAG
from domains.learning.service import quiz_judge

ITEMS = [
    {"item_id": 11, "obj": "이거 얼마예요?", "des": "How much is it?", "ex": None},
    {"item_id": 12, "obj": "잘 부탁드립니다", "des": "Please take good care of me", "ex": None},
    {"item_id": 13, "obj": "저는 ◯◯ 사람이에요", "des": "I'm from ◯◯", "ex": None},
    {"item_id": 14, "obj": "도와주세요", "des": "Please help me", "ex": None},
    {"item_id": 15, "obj": "처음 뵙겠습니다", "des": "How do you do?", "ex": None},
    {"item_id": 16, "obj": "◯◯이/가 뭐예요?", "des": "What is ◯◯?", "ex": None},
    {"item_id": 17, "obj": "네", "des": "yes", "ex": None},
]


def _state(n: int = 7) -> cs._CallState:
    st = cs._CallState()
    st.expr_items = list(ITEMS[:n])
    st.reground_items = [i["obj"] for i in ITEMS[:n]]
    st.expr_ctx = {"client": object(), "model": "m", "locale_label": "영어(English)", "target_language": "한국어"}
    st.reground_persona = ("선생님", "다정함")
    return st


def _beaver(st: cs._CallState, text: str) -> None:
    st.cur_beaver_text = [text]
    cs._flush_beaver_segment(st)


def _user(st: cs._CallState, text: str) -> None:
    st.cur_user_text = [text]
    cs._flush_user_segment(st)


class _Sess:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[str, bool]] = []
        self.fail = fail

    async def send_reground(self, text, *, turn_complete=True):
        if self.fail:
            raise RuntimeError("전송 실패")
        self.sent.append((text, turn_complete))


def _voiced() -> bytes:
    import struct
    return b"".join(struct.pack("<h", 12000 if i % 2 else -12000) for i in range(320))


# --------------------------------------------------------------------------- #
# 상태기계 — arm
# --------------------------------------------------------------------------- #
def test_no_cue_before_the_group_is_full() -> None:
    st = _state()
    _beaver(st, '오늘 첫 표현은 "이거 얼마예요?"')
    _beaver(st, '다음은 "잘 부탁드립니다"')
    assert st.covered_nums == [1, 2] and st.expr_quiz_cue_pending is None


def test_three_covered_arms_one_cue_with_the_control_tag() -> None:
    """3개가 차면 큐 하나 — CONTROL_TAG 접두(⛔ «[시스템]» 은 종료 태그와 같던 시절 태그, call 706), 항목 라벨, 개수."""
    st = _state()
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"저는 미국 사람이에요"'):
        _beaver(st, t)
    cue = st.expr_quiz_cue_pending
    assert cue is not None and cue.startswith(CONTROL_TAG + " 지금 퀴즈를 내라")
    assert "[시스템]" not in cue
    for lab in ("«이거 얼마예요?»", "«잘 부탁드립니다»", "«저는 ◯◯ 사람이에요»"):
        assert lab in cue
    assert "3개를 한 문제씩" in cue and "정답을 먼저 말하지 마라" in cue
    assert st.expr_quiz_set == [1, 2, 3] and st.expr_quiz_seq == 1
    assert st.reground_pending is False and not st.reground_reminder, "큐는 재접지 슬롯을 쓰지 않는다"


def test_the_placeholder_item_is_covered_by_the_learners_utterance() -> None:
    """bt-back 회귀 — 「저는 ◯◯ 사람이에요」 가 학습자 발화로 covered 돼야 3개가 차고 큐가 열린다(mentions 템플릿 인식)."""
    st = _state()
    _user(st, "저는 미국 사람이에요.")
    assert st.covered_nums == [3]
    _user(st, "이게 뭐예요?")
    assert st.covered_nums == [3, 6]


def test_a_second_cue_waits_until_the_first_quiz_is_closed() -> None:
    st = _state()
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"저는 미국 사람이에요"'):
        _beaver(st, t)
    assert st.expr_quiz_seq == 1
    # 큐 대기 중에 비버가 4·5·6 을 더 말해도(드릴 계속) 두 번째 큐는 안 선다
    for t in ('"도와주세요"', '"처음 뵙겠습니다"', '"이게 뭐예요?"'):
        _beaver(st, t)
    assert st.expr_quiz_seq == 1 and st.expr_quiz_set == [1, 2, 3]


def test_the_tail_gets_a_cue_even_below_the_group_size() -> None:
    """⛔⛔ P0-3 — 목록 끝에 3개가 안 남아도 남은 것만으로 큐를 연다(비버가 스스로 열지 않으니 서버가)."""
    st = _state(4)
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"저는 미국 사람이에요"'):
        _beaver(st, t)
    _open_and_close(st)                     # 1~3 퀴즈 끝
    _beaver(st, '"도와주세요"')             # 4 = 마지막 항목, 묶음이 안 찬다
    assert st.expr_quiz_cue_pending is not None and st.expr_quiz_set == [4]


def _open_and_close(st: cs._CallState, *, lines: list[tuple[str, str]] | None = None) -> None:
    """큐 얹힘 → 비버 턴 시작(창 열림) → 창 안 발화 → 닫힘(비버가 다음 항목 소개)을 흉내 낸다."""
    st.expr_quiz_cue_pending = None
    st.expr_quiz_awaiting_open = True
    cs._expression_quiz_open_on_beaver_turn(st)
    for role, text in lines or []:
        (_beaver if role == "B" else _user)(st, text)
    cs._close_expression_quiz(st, why="시험")


# --------------------------------------------------------------------------- #
# 상태기계 — 얹기(재접지 자리·RMS 2관문만 빌린다)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_cue_rides_the_mic_gate_and_leaves_the_reground_counters_alone(caplog) -> None:
    """⛔ fable P1-3 — 큐가 reground_count/last_reground_ts 를 건드리면 재접지 간격 150s 를 소모해 쪽지가 0회가 된다."""
    st = _state()
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"저는 미국 사람이에요"'):
        _beaver(st, t)
    st.reground_pending = True
    st.reground_reminder = "재접지 쪽지"
    st.reground_count = 1
    st.last_reground_ts = 123.0
    sess = _Sess()
    with caplog.at_level(logging.INFO, logger=cs.logger.name):
        await cs._maybe_attach_reground_on_mic(sess, st, _voiced())
    assert len(sess.sent) == 1 and sess.sent[0][0].startswith(CONTROL_TAG + " 지금 퀴즈를 내라")
    assert sess.sent[0][1] is False, "turn_complete=False — 학습자 발화에 얹는다(재접지와 같은 통로)"
    assert st.expr_quiz_cue_pending is None and st.expr_quiz_awaiting_open is True
    assert st.reground_pending is True and st.reground_reminder == "재접지 쪽지", "재접지는 다음 발화에 보낸다"
    assert st.reground_count == 1 and st.last_reground_ts == 123.0
    assert any(r.getMessage().startswith(cs.EXPR_QUIZ_CUE_LOG_PREFIX + " 얹기") for r in caplog.records), \
        "하네스 계약 — 로그 접두 «normalcall 표현학습 퀴즈 큐»"
    # 다음 발화에서 재접지가 나간다
    await cs._maybe_attach_reground_on_mic(sess, st, _voiced())
    assert len(sess.sent) == 2 and sess.sent[1][0] == "재접지 쪽지" and st.reground_count == 2


@pytest.mark.asyncio
async def test_a_failed_cue_send_keeps_the_cue_pending() -> None:
    """⛔ 재접지의 «await 전 내림» 과 다르다 — 큐는 필수라 다음 발화에서 재시도한다."""
    st = _state()
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"저는 미국 사람이에요"'):
        _beaver(st, t)
    await cs._maybe_attach_reground_on_mic(_Sess(fail=True), st, _voiced())
    assert st.expr_quiz_cue_pending is not None and st.expr_quiz_awaiting_open is False


@pytest.mark.asyncio
async def test_silence_and_closing_never_attach_the_cue() -> None:
    st = _state()
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"저는 미국 사람이에요"'):
        _beaver(st, t)
    sess = _Sess()
    await cs._maybe_attach_reground_on_mic(sess, st, b"\x00\x00" * 320)
    assert sess.sent == [] and st.expr_quiz_cue_pending is not None
    st.should_close = True
    await cs._maybe_attach_reground_on_mic(sess, st, _voiced())
    assert sess.sent == []


def test_the_quiz_log_prefix_is_fixed_for_the_harness() -> None:
    assert cs.EXPR_QUIZ_CUE_LOG_PREFIX == "normalcall 표현학습 퀴즈 큐"


# --------------------------------------------------------------------------- #
# 상태기계 — 열림(open_seg) · 닫힘
# --------------------------------------------------------------------------- #
def test_the_window_opens_at_the_next_beaver_turn_not_at_the_cue() -> None:
    """⛔ fable P1-1 — 큐를 태운 학습자 발화(드릴 3번 실패 뒤 복창이 지배적 경로)가 창 첫 U 가 되면 공개 B 는 창 밖이라
    앵무새가 통과한다. open_seg 는 큐 얹힘 뒤 **다음 비버 turn_start(flush 직후)** 의 len(segments) 다."""
    st = _state()
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"저는 미국 사람이에요"'):
        _beaver(st, t)
    st.expr_quiz_cue_pending = None
    st.expr_quiz_awaiting_open = True          # 얹힘 — 이 발화는 학습자 복창 «저는 미국 사람이에요»
    _user(st, "저는 미국 사람이에요")          # 비버 turn_start 에서 flush 된 직전 U
    cs._expression_quiz_open_on_beaver_turn(st)
    assert st.expr_quiz_open is True and st.expr_quiz_open_seg == len(st.segments) == 4
    # 창 안에는 이제 퀴즈만 — 복창은 창 밖이라 3번은 통과가 아니다
    _beaver(st, "Quiz time! How do you say I'm from America?")
    _user(st, "음...")
    _beaver(st, 'It\'s "저는 미국 사람이에요". Now next: "도와주세요"')   # 공개 + 다음 항목 소개 → 닫힘
    assert st.expr_quiz_open is False
    assert 13 not in st.expr_quiz_pass and 13 in st.expr_quiz_fail


def test_the_quiz_closes_when_the_beaver_introduces_the_next_item() -> None:
    st = _state()
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"저는 미국 사람이에요"'):
        _beaver(st, t)
    _open_and_close(st, lines=[("B", "Quiz! How much is it?"), ("U", "이거 얼마예요?"),
                               ("B", "Great! Please take good care of me?"), ("U", "잘 부탁드립니다")])
    # _open_and_close 가 닫았다 — 여기서는 닫힘 «조건» 자체를 본다: 다음 시나리오
    st2 = _state()
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"저는 미국 사람이에요"'):
        _beaver(st2, t)
    st2.expr_quiz_cue_pending = None
    st2.expr_quiz_awaiting_open = True
    cs._expression_quiz_open_on_beaver_turn(st2)
    _beaver(st2, "Quiz! How much is it?")
    _user(st2, "이거 얼마예요?")
    assert st2.expr_quiz_open is True
    _beaver(st2, 'Good. New phrase: "도와주세요"')      # 4 = 아직 안 다룬 가장 앞 번호 → 닫힘
    assert st2.expr_quiz_open is False and 11 in st2.expr_quiz_pass


def test_a_learner_saying_an_outside_item_does_not_close_the_quiz() -> None:
    """⛔ codex P1-1 — 학습자가 오답으로 4번 표면형을 말하면 1~3 퀴즈가 조기 닫히던 것. 닫힘은 비버 발화만."""
    st = _state()
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"저는 미국 사람이에요"'):
        _beaver(st, t)
    st.expr_quiz_cue_pending = None
    st.expr_quiz_awaiting_open = True
    cs._expression_quiz_open_on_beaver_turn(st)
    _beaver(st, "Quiz! How much is it?")
    _user(st, "도와주세요")                     # 오답 — 4번 표면형
    assert st.expr_quiz_open is True and 4 in st.covered_nums and 4 not in st.expr_covered_by_beaver


def test_a_confused_reveal_of_a_far_item_does_not_close_but_two_strays_do() -> None:
    """fable P1-2 — J(항목 혼동: 묻던 것과 다른 표면형 공개)는 «순번상 다음 항목» 이 아니라 안 닫힌다. 안전판: 밖 번호 2개."""
    st = _state()
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"저는 미국 사람이에요"'):
        _beaver(st, t)
    st.expr_quiz_cue_pending = None
    st.expr_quiz_awaiting_open = True
    cs._expression_quiz_open_on_beaver_turn(st)
    _beaver(st, "Quiz! How much is it?")
    _beaver(st, 'Hmm, it\'s "처음 뵙겠습니다"... no wait.')    # 5번 — 다음 항목(4) 아님 → 안 닫힘
    assert st.expr_quiz_open is True and st.expr_quiz_stray == [5]
    _beaver(st, 'Anyway, "이게 뭐예요?"')                      # 6번 — 밖 번호 2개째 → 안전판 닫힘
    assert st.expr_quiz_open is False


def test_a_retry_cue_is_armed_once_for_failed_items_after_everything_is_covered() -> None:
    st = _state(3)
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"저는 미국 사람이에요"'):
        _beaver(st, t)
    _open_and_close(st, lines=[("B", "Quiz! How much is it?"), ("U", "모르겠어요"), ("B", 'It\'s "이거 얼마예요?"'),
                               ("B", "Next: please take care?"), ("U", "잘 부탁드립니다"),
                               ("B", "I'm from America?"), ("U", "저는 미국 사람이에요")])
    assert st.expr_quiz_fail == {11} and st.expr_quiz_pass == {12, 13}
    assert st.expr_quiz_cue_pending is not None and "아까 틀린" in st.expr_quiz_cue_pending
    assert st.expr_quiz_set == [1] and st.expr_retry_cued is True
    _open_and_close(st, lines=[("B", "Again: how much is it?"), ("U", "이거 얼마예요?")])
    assert st.expr_quiz_pass == {11, 12, 13} and st.expr_quiz_fail == set()
    assert st.expr_quiz_cue_pending is None, "오답 재출제는 통화당 1회"


# --------------------------------------------------------------------------- #
# 서버 판정 — 첫 사건 규칙
# --------------------------------------------------------------------------- #
def _judge(lines: list[tuple[str, str]], quiz_set=(1, 2, 3), n=7):
    st = _state(n)
    span = [(i, "beaver" if r == "B" else "user", t) for i, (r, t) in enumerate(lines)]
    res = cs._server_judge_quiz(st, span, list(quiz_set))
    return st, res


def test_learner_first_then_formal_is_passed() -> None:
    st, res = _judge([("B", "How much is it?"), ("U", "이거 얼마예요?"), ("B", "Great!")], quiz_set=(1,))
    assert res["passed"] == [1] and st.expr_quiz_pass == {11}


def test_beaver_reveal_first_is_failed_even_if_the_learner_parrots() -> None:
    """A·E 유형 — 공개 뒤 복창은 failed. 창 안이라 드릴/퀴즈를 못 가르던 옛 문제가 없다."""
    st, res = _judge([("B", "How much is it?"), ("U", "음..."), ("B", 'It\'s "이거 얼마예요?"'), ("U", "이거 얼마예요?")],
                     quiz_set=(1,))
    assert res["failed"] == [1] and st.expr_quiz_fail == {11} and st.expr_quiz_pass == set()


def test_an_informal_answer_is_not_an_event_and_the_reveal_makes_it_failed() -> None:
    """B 유형(1398 t9 「잘 못 들었다」) — 반말 산출은 사건이 아니다. 뒤에 공개가 오면 failed, 안 오면 미판정."""
    st, res = _judge([("B", "How much?"), ("U", "이거 얼마야"), ("B", 'Polite! "이거 얼마예요?"')], quiz_set=(1,))
    assert res["failed"] == [1]
    st2, res2 = _judge([("B", "How much?"), ("U", "이거 얼마야"), ("B", "Hmm.")], quiz_set=(1,))
    assert res2["pending"] == [1] and st2.expr_quiz_pass == set() and st2.expr_quiz_fail == set()


def test_no_mention_at_all_is_pending_not_failed() -> None:
    """⛔ 침묵 ≠ 오답 — 미판정으로 남는다(다음 통화에 다시 나온다)."""
    st, res = _judge([("B", "How much?"), ("U", "(무음/전사없음)")], quiz_set=(1,))
    assert res["pending"] == [1] and st.expr_quiz_fail == set()


def test_a_template_item_passes_when_filled_in() -> None:
    st, res = _judge([("B", "I'm from America?"), ("U", "저는 미국 사람이에요")], quiz_set=(3,))
    assert res["passed"] == [3] and st.expr_quiz_pass == {13}


def test_passed_is_never_demoted_by_a_later_quiz() -> None:
    st = _state()
    st.expr_quiz_pass.add(11)
    span = [(0, "beaver", "How much?"), (1, "beaver", 'It\'s "이거 얼마예요?"')]
    res = cs._server_judge_quiz(st, span, [1])
    assert res["failed"] == [1] and st.expr_quiz_pass == {11} and st.expr_quiz_fail == set()


def test_a_wrong_answer_that_is_another_item_is_not_this_items_event() -> None:
    st, res = _judge([("B", "How much?"), ("U", "도와주세요"), ("B", 'No — "이거 얼마예요?"')], quiz_set=(1,))
    assert res["failed"] == [1]


# --------------------------------------------------------------------------- #
# STT 폴백 — 미판정 항목만 · 서버 3조건 검증
# --------------------------------------------------------------------------- #
def _span(lines):
    return [(i, "beaver" if r == "B" else "user", t) for i, (r, t) in enumerate(lines)]


def test_fallback_verification_requires_a_user_segment_inside_the_window() -> None:
    st = _state()
    span = _span([("B", "This one please?"), ("U", "이거 지세요"), ("B", "Good, next.")])
    ok, _ = cs._verify_stt_fallback(st, span, 4, 1)
    assert ok
    assert cs._verify_stt_fallback(st, span, 4, 0)[0] is False, "B 세그먼트"
    assert cs._verify_stt_fallback(st, span, 4, 9)[0] is False, "창 밖"
    assert cs._verify_stt_fallback(st, span, 4, None)[0] is False


def test_fallback_verification_rejects_a_reveal_before_the_answer() -> None:
    st = _state()
    span = _span([("B", 'Say "도와주세요"'), ("U", "도와 지세요"), ("B", "Good")])
    ok, why = cs._verify_stt_fallback(st, span, 4, 1)
    assert not ok and "공개" in why


def test_fallback_verification_rejects_when_the_beaver_corrects_right_after() -> None:
    """«그 다음 B 에도 표면형 없음» — 비버가 정정했다면 소리를 들은 쪽이 틀렸다고 증언한 것이다."""
    st = _state()
    span = _span([("B", "Please help me?"), ("U", "도와 지세요"), ("B", 'Almost — "도와주세요"')])
    ok, why = cs._verify_stt_fallback(st, span, 4, 1)
    assert not ok and "정정" in why


@pytest.mark.asyncio
async def test_the_fallback_is_called_only_for_pending_items_and_writes_with_provenance(monkeypatch, caplog) -> None:
    st = _state()
    span = _span([("B", "This one please?"), ("U", "이거 지세요"), ("B", "Good. Next one.")])
    seen: dict = {}

    async def _llm(client, model, **kw):
        seen["instruction"] = kw["system_instruction"]
        seen["prompt"] = kw["prompt"]
        return cs.ExpressionQuizOut(items=[cs.ExpressionQuizFallbackItem(num=4, answer_seg=1),
                                          cs.ExpressionQuizFallbackItem(num=1, answer_seg=1)])   # 1 은 pending 아님 → 무시

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _llm)
    with caplog.at_level(logging.INFO, logger=cs.logger.name):
        got = await cs._expression_quiz_stt_fallback(st, span, [4])
    assert got == [4] and st.expr_quiz_pass == {14} and 11 not in st.expr_quiz_pass
    assert "4. 도와주세요" in seen["instruction"] and "1. 이거 얼마예요?" not in seen["instruction"], "미판정 항목만"
    assert "U1: 이거 지세요" in seen["prompt"] and "B0: This one please?" in seen["prompt"]
    assert any("provenance=stt_fallback" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_fallback_is_skipped_without_a_user_line_or_pending_items(monkeypatch) -> None:
    st = _state()
    called = []

    async def _llm(*a, **k):
        called.append(1)
        return None

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _llm)
    assert await cs._expression_quiz_stt_fallback(st, _span([("B", "Quiz?")]), [4]) == []
    assert await cs._expression_quiz_stt_fallback(st, _span([("B", "Quiz?"), ("U", "네")]), []) == []
    assert called == []


@pytest.mark.asyncio
async def test_a_fallback_failure_leaves_the_item_pending(monkeypatch) -> None:
    st = _state()

    async def _boom(*a, **k):
        raise RuntimeError("폭발")

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _boom)
    got = await cs._expression_quiz_stt_fallback(st, _span([("B", "Q?"), ("U", "이거 지세요")]), [4])
    assert got == [] and st.expr_quiz_pass == set() and st.expr_quiz_fail == set()


# --------------------------------------------------------------------------- #
# 종료(닫힘 ②) — 마지막 판정은 열린 창만 · 서버 검색은 즉시 · 폴백만 상한
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_final_judgement_closes_an_open_quiz_including_the_unflushed_tail(monkeypatch) -> None:
    st = _state()
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"저는 미국 사람이에요"'):
        _beaver(st, t)
    st.expr_quiz_cue_pending = None
    st.expr_quiz_awaiting_open = True
    cs._expression_quiz_open_on_beaver_turn(st)
    _beaver(st, "Quiz! How much is it?")
    st.cur_user_text = ["이거 얼마예요?"]           # 아직 flush 안 된 꼬리
    called = []

    async def _llm(*a, **k):
        called.append(1)
        return None

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _llm)
    await cs._final_expression_progress(st)
    assert st.expr_quiz_open is False and 11 in st.expr_quiz_pass
    assert called == [1], "미판정(2·3)이 있으니 폴백은 한 번 부른다"


@pytest.mark.asyncio
async def test_a_stalled_fallback_never_blocks_the_write(monkeypatch) -> None:
    st = _state()
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"저는 미국 사람이에요"'):
        _beaver(st, t)
    st.expr_quiz_cue_pending = None
    st.expr_quiz_awaiting_open = True
    cs._expression_quiz_open_on_beaver_turn(st)
    _beaver(st, "Quiz!")
    _user(st, "음")

    async def _hang(*a, **k):
        await asyncio.sleep(10)

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _hang)
    monkeypatch.setattr(cs, "EXPR_FINAL_JUDGE_TIMEOUT_S", 0.05)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await cs._final_expression_progress(st)
    assert loop.time() - t0 < 1.0 and st.expr_quiz_open is False


@pytest.mark.asyncio
async def test_an_unattached_cue_at_call_end_leaves_only_drilled(monkeypatch) -> None:
    """codex P1-2 — idle 폴백을 두지 않는다: 얹히지 않은 큐는 drilled 로만 남는다(안전 방향)."""
    st = _state()
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"저는 미국 사람이에요"'):
        _beaver(st, t)
    called = []

    async def _llm(*a, **k):
        called.append(1)

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _llm)
    await cs._final_expression_progress(st)
    assert st.expr_quiz_cue_pending is not None and called == []
    assert st.expr_quiz_pass == set() and st.expr_quiz_fail == set() and cs._expr_covered_ids(st) == [11, 12, 13]


def test_no_old_drill_sidecar_remains() -> None:
    """§5 — 옛 판정 경로가 살아 있으면 두 판정이 섞인다. 함수·스키마·슬롯이 전부 없어야 한다."""
    for name in ("_expression_progress_sidecar", "_spawn_expression_progress", "_expression_progress_instruction",
                 "ExpressionProgressOut", "_apply_expression_progress", "_expression_transcript_window",
                 "EXPR_JUDGE_OVERLAP_SEGMENTS", "EXPR_WINDOW_BOUNDARY_LINE"):
        assert not hasattr(cs, name), name
    st = cs._CallState()
    for slot in ("expr_judged_upto", "expr_phase", "expr_covered_snapshot"):
        assert not hasattr(st, slot), slot
    src = pathlib.Path(cs.__file__).read_text(encoding="utf-8")
    assert "_spawn_expression_progress(" not in src


# --------------------------------------------------------------------------- #
# quiz_judge — 템플릿 대조 · 격식 표지
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,surface,exp", [
    ("저는 미국 사람이에요.", "저는 ◯◯ 사람이에요", True),
    ("나는 미국 사람", "저는 ◯◯ 사람이에요", False),
    ("이게 뭐예요?", "◯◯이/가 뭐예요?", True),
    ("사과가 뭐예요", "◯◯이/가 뭐예요?", True),
    ("물이 있어요", "N이/가 있어요[없어요]", True),
    ("물이 없어요", "N이/가 있어요[없어요]", False),          # 대괄호 대안은 주형만 인정 — 미판정 방향(안전)
    ("이거는 책이에요", "이거는[그거는, 저거는]N이에요/예요", True),
    ("이거는 사과예요", "이거는[그거는, 저거는]N이에요/예요", True),
    ("학교에 가요", "N에 가요[와요]", True),
    ("친구 앞에 있어요", "N 앞[뒤, 옆]", True),
    ("사과 두 개", "N개[병, 잔, 그릇]", True),
    ("How do you say I am from Country", "저는 ◯◯ 사람이에요", False),
    ("이거 얼마예요?", "이거 얼마예요?", True),                  # 템플릿 아님 — 기존 경로
    ("선물이에요", "물", False),
    ("개나리가 피었어요", "개", False),
])
def test_template_aware_mentions(text: str, surface: str, exp: bool) -> None:
    assert quiz_judge.mentions(text, surface) is exp


@pytest.mark.parametrize("surface,marker", [
    ("이거 얼마예요?", "요"), ("잘 부탁드립니다", "니다"), ("도와주세요", "요"), ("안녕히 가십시오", "십시오"),
    ("그렇죠", "죠"), ("N에 가요[와요]", "요"), ("네", None), ("가다", None), ("화장실이 어디야?", None),
])
def test_polite_marker_is_the_final_ending_of_the_surface(surface: str, marker) -> None:
    assert quiz_judge.polite_marker(surface) == marker


@pytest.mark.parametrize("text,surface,exp", [
    ("잘 못 들었다", "잘 못 들었어요", False),          # 반말 ①
    ("안녕", "안녕하세요", False),                    # ②
    ("이거 얼마야?", "이거 얼마예요?", False),         # ③
    ("나는 미국 사람", "저는 ◯◯ 사람이에요", False),   # ④
    ("잘 부탁드려", "잘 부탁드립니다", False),         # ⑤
    ("이거 얼마예요", "이거 얼마예요?", True),          # 표지 있음(문장부호 무관)
    ("요리 좋아", "도와주세요", False),                # «요» 는 어절 끝만
    ("물을 주세요", "물", True),                      # 표지 없는 항목은 항상 참
    ("이거 주세요 좀", "이거 주세요", True),           # 조사·어순 허용
])
def test_keeps_formality(text: str, surface: str, exp: bool) -> None:
    assert quiz_judge.keeps_formality(text, surface) is exp


def test_stt_misspelling_is_pending_not_passed_by_string_match() -> None:
    """하네스 M — 「이거 주세요」→「이거 지세요」 3/3. 글자 대조는 거짓(미판정)이고, 폴백이 그 자리를 맡는다."""
    assert quiz_judge.mentions("이거 지세요", "이거 주세요") is False
