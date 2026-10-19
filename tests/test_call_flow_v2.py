"""통화 흐름 v2(2026-10-16 · T-20261016-03 · PM-DEC-682·684) — 단계 추적 reveal·repeat · [차시 안내] · 프리토킹 2문장.

근거: 52 제품프롬프트설계 하네스 `2026-10-14_통화흐름_공동설계/16_공유_표현학습_잠금변경제안.md` §3.1·§3.2·§4(SV-01) · `13_공유_프리토킹.md` §9(SV-34).
"""
from __future__ import annotations

from core.prompts.chat import build_chat_instruction
from core.prompts.expression import build_expression_instruction
from core.prompts.locked.expression import lesson_guide_line
from domains.learning.realtime.expression_practice import ExpressionPractice

ITEMS = [{"item_id": 1, "obj": "감사합니다", "des": "Thank you"}, {"item_id": 2, "obj": "안녕하세요", "des": "Hello"}]


def test_learner_answer_without_beaver_saying_the_item_counts_as_the_first_attempt() -> None:
    # 비버는 표현을 먼저 말하지 않는다 — introduce 에서 온 답도 원문 시도다
    p = ExpressionPractice(ITEMS)
    p.observe_beaver("고마울 때 뭐라고 해요?")
    assert p.phase == "introduce"
    p.observe_user("감사합니다")
    assert p.phase == "native_intro" and p.original_attempts == 1


def test_full_regular_cycle_original_hint_reveal_native_repeat_next() -> None:
    p = ExpressionPractice(ITEMS)
    p.observe_user("모르겠어요")                      # 1회째 오답 → 힌트 뒤 재도전
    assert p.phase == "original" and "힌트 뒤 답 대기" in p.brief()
    p.observe_user("감사")                            # 2회째 오답 → 정답 따라 말하기
    assert p.phase == "reveal" and "정답 따라 말하기 대기" in p.brief()
    p.observe_user("감사합니다")                      # 결과와 무관하게 현지인 문장
    assert p.phase == "native_intro"
    p.observe_beaver('자연스럽게는 "정말 고마워요"라고 해요. 따라 해 보세요.')
    assert p.phase == "native" and p.native_phrase == "정말 고마워요"
    p.observe_user("정말 고마워요")
    assert p.phase == "repeat" and "같은 상황을 상대만 바꿔 다시 주고 «정말 고마워요»" in p.brief()
    p.observe_user("감사합니다")                      # 원문으로 답해도 맞음 · 답 1회 뒤 다음
    assert p.index == 1 and p.completed == {1} and p.phase == "introduce"
    assert "알려주지 말고 뜻·상황만" in p.brief()


def test_brief_says_not_knowing_is_not_mocked_and_reveal_stops_the_turn() -> None:
    """D1(b)·D2(PM-DEC-691 · call 1993) — 모름·무응답은 놀리지 않는다 · 정답을 들려준 턴은 거기서 멈춘다."""
    p = ExpressionPractice(ITEMS)
    assert p.phase == "introduce"
    p.observe_beaver("고마울 때 뭐라고 해요?")
    p.observe_user("모르겠어요")
    assert p.phase == "original" and p.original_attempts == 1
    b1 = p.brief()
    assert "또 틀리거나 모르면 정답 «감사합니다»을 들려주고 한 번 따라 말하게 한 뒤 그 턴을 멈춘다." in b1
    assert b1.count("모름·무응답은 놀리지 않는다.") == 1
    p0 = ExpressionPractice(ITEMS)
    p0.phase = "original"
    assert "틀리거나 모르면 첫 어절 힌트를 주고 한 번 더 묻는다. 모름·무응답은 놀리지 않는다." in p0.brief()


def test_homework_cycle_has_no_native_but_keeps_one_repeat() -> None:
    p = ExpressionPractice(ITEMS, native_step=False)
    p.observe_user("감사합니다")
    assert p.phase == "repeat" and "«감사합니다»" in p.brief() and "현지인 표현은 만들지 않는다" in p.brief()
    p.observe_user("감사합니다")
    assert p.index == 1


def test_every_new_phase_is_pending() -> None:
    p = ExpressionPractice(ITEMS)
    for phase in ("original", "reveal", "native_intro", "native", "repeat"):
        p.phase = phase
        assert p.pending, phase
    p.phase = "introduce"
    assert not p.pending


def test_lesson_guide_line_only_when_passed() -> None:
    kw = dict(role="r", personality="p", level_profile="초급이다.", locale="en", interests=[], items=ITEMS, quiz_group=3)
    base = build_expression_instruction(**kw)
    assert "[차시 안내]" not in base
    out = build_expression_instruction(**kw, lesson_guide=("가족과 친척", "가족 소개하기"))
    assert "[학습자 수준] 초급이다.\n[차시 안내] 주제: 가족과 친척 · 상황: 가족 소개하기\n" in out
    assert out.replace("[차시 안내] 주제: 가족과 친척 · 상황: 가족 소개하기\n", "", 1) == base
    assert lesson_guide_line(None, "인사하기") == "[차시 안내] 상황: 인사하기"
    assert lesson_guide_line(None, " ") == ""


def test_freetalk_chat_reply_cap_is_two_sentences_when_passed() -> None:
    a = dict(role="r", personality="p", level_profile="", locale="en", interests=[], close_tag="[통화종료]")
    assert "매 응답은 1~4문장" in build_chat_instruction(**a)
    out = build_chat_instruction(**a, max_sentences=2)
    assert "매 응답은 1~2문장" in out and "1~4문장" not in out


def test_freetalk_reground_keeps_the_topic_and_normal_chat_is_unchanged() -> None:
    # PM-DEC-685 R1 — 프리토킹(chat · 차시 없는 freetalk)만 화제 유지 끝줄 · 일반 chat 문장은 그대로
    from core.prompts.locked.reground import build_reground_brief
    ft = build_reground_brief("r", "p", mode="freetalk")
    assert "지금 화제를 그대로 이어가라" in ft and "화제는 네가 바꾸지 마라" in ft
    assert "흥미를 느낄 새 질문" not in ft
    chat = build_reground_brief("r", "p", mode="chat")
    assert "학습자가 흥미를 느낄 새 질문을 하나 던져 대화를 더 끌고 가라" in chat and "지금 화제를 그대로" not in chat


# --------------------------------------------------------------------------- #
# v2 재배포 C1·C2·P1·L11(2026-10-16 PM-DEC-698·699 · 23_공유_v2재배포계획) — 학습자 발화마다 코스별 고정 안내
# --------------------------------------------------------------------------- #
import asyncio as _asyncio
import hashlib as _hashlib

import pytest as _pytest

import domains.learning.realtime.call_session as _cs
from core.prompts.homework import HOMEWORK_NO_NATIVE_LINE as _L11
from core.prompts.locked import seeds as _seeds
from core.prompts.locked.rules import CONTROL_TAG as _CONTROL_TAG
from core.prompts.editable_loader import BANNED_WORDS as _BANNED


class _Rec:
    def __init__(self):
        self.sent: list[tuple[str, bool]] = []

    async def send_reground(self, text, *, turn_complete=True):
        self.sent.append((text, turn_complete))


def _st(note):
    st = _cs._CallState()
    st.turn_note = note
    st.beaver_turns = 1          # 첫 비버 턴(인사·출제)이 끝났다
    return st


def _loud():
    import struct
    return struct.pack("<" + "h" * 480, *([12000, -12000] * 240))


def test_turn_notes_are_control_tagged_fixed_and_banned_word_free():
    notes = (_seeds.TURN_NOTE_EXPRESSION, _seeds.TURN_NOTE_HOMEWORK_EXPRESSION, _seeds.TURN_NOTE_FREETALK)
    for n in notes:
        assert n.startswith(_CONTROL_TAG + " ") and "두 문장" in n
        assert not [w for w in _BANNED if w in n]
    assert "현지인 문장을 따라 말하게" in notes[0] and "현지인 문장은 없다" in notes[1]
    for n in notes[:2]:   # 25_공유_오독결과 §3 2차 — 나열형은 다시 말하기 0/4 · 조건형 3/4
        assert "바로 새 표현으로 가지 말고" in n
    assert "모름·무응답에는 놀리지 말고 힌트만 준다" in notes[1]
    # 33_공유_P2재생 §3 (PM-DEC-757) — 표현학습만 P2 보강 · 숙제는 그대로
    assert "모름·무응답에는 모국어로 말해도 놀리지 말고 바로 힌트만 준다" in notes[0]
    assert "같은 문장을 상대만 바꿔 한 번 더 말하게 한다(새 질문을 만들지 않는다)" in notes[0]
    # 39_공유_자유대화배포 §3 (PM-DEC-809) — 자유대화 안내는 「놀림」 금지를 뺐다(말투대로 반응) · 욕설·비하어 금지는 남는다
    assert "욕설·비하어는 쓰지 않는다" in notes[2] and "놀림" not in notes[2]


def test_l11_bracket_is_limited_to_the_repeat_step():
    assert "(이 다시 말하기에서 틀리면 힌트 없이 정답을 한 번 들려주고 넘어간다)" in _L11
    assert _hashlib.sha256(_L11.encode("utf-8")).hexdigest()[:16] == "0ace008d01331d01"


@_pytest.mark.parametrize("args,want", [
    (("expression", False, "", True, False), "expr"),
    (("expression", False, "", False, False), None),
    (("homework", True, "expression", True, False), "hw"),
    (("homework", True, "roleplay", True, False), None),        # 회화(역할극 숙제) 제외
    (("chat", False, "", False, False), "free"),
    (("freetalk", False, "", False, False), "free"),
    (("freetalk", False, "", False, True), None),               # 회화학습(차시 프리토킹) 제외
    (("level_test", False, "", False, False), None),
])
def test_turn_note_course_selection(args, want):
    got = _cs._turn_note_for(*args)
    kinds = {_seeds.TURN_NOTE_EXPRESSION: "expr", _seeds.TURN_NOTE_HOMEWORK_EXPRESSION: "hw", _seeds.TURN_NOTE_FREETALK: "free", None: None}
    assert kinds[got] == want


def test_turn_note_attaches_once_per_learner_turn_with_turn_complete_false():
    st, s = _st(_seeds.TURN_NOTE_FREETALK), _Rec()
    for _ in range(4):                                         # 같은 학습자 턴의 연속 음성 프레임
        _asyncio.run(_cs._maybe_attach_reground_on_mic(s, st, _loud()))
    assert s.sent == [(_seeds.TURN_NOTE_FREETALK, False)], "학습자 턴당 1회 · 완결 텍스트 턴 금지(이중 발화)"
    st.turn_note_sent = False                                  # 비버 턴 시작 = 다음 학습자 턴(펌프가 내린다)
    _asyncio.run(_cs._maybe_attach_reground_on_mic(s, st, _loud()))
    assert len(s.sent) == 2


def test_turn_note_is_not_sent_while_beaver_speaks_or_closing_or_silent():
    for setup in ({"turn_id": "t1"}, {"should_close": True}, {"close_seed_sent": True}):
        st, s = _st(_seeds.TURN_NOTE_EXPRESSION), _Rec()
        for k, v in setup.items():
            setattr(st, k, v)
        _asyncio.run(_cs._maybe_attach_reground_on_mic(s, st, _loud()))
        assert s.sent == [], setup
    st, s = _st(_seeds.TURN_NOTE_EXPRESSION), _Rec()
    _asyncio.run(_cs._maybe_attach_reground_on_mic(s, st, b"\x00\x00" * 480))   # 침묵 프레임 — 사람이 말할 때까지 기다린다
    assert s.sent == [] and st.turn_note_sent is False


def test_turn_note_waits_for_the_first_beaver_turn_and_obeys_the_env_switch(monkeypatch):
    st, s = _st(_seeds.TURN_NOTE_EXPRESSION), _Rec()
    st.beaver_turns = 0                                        # 인사 전(call 2000 06:27:13 · 연결 6ms 뒤)
    _asyncio.run(_cs._maybe_attach_reground_on_mic(s, st, _loud()))
    assert s.sent == [] and st.turn_note_sent is False
    monkeypatch.setattr(_cs, "TURN_NOTE_MODE", "off")
    st.beaver_turns = 1
    _asyncio.run(_cs._maybe_attach_reground_on_mic(s, st, _loud()))
    assert s.sent == [], "LIVE_TURN_NOTE=off 면 안내 0"


def test_expression_turn_note_is_skipped_in_the_quiz_window_but_freetalk_is_not():
    for flag in ("expr_quiz_open", "expr_quiz_awaiting_open"):
        st, s = _st(_seeds.TURN_NOTE_EXPRESSION), _Rec()
        setattr(st, flag, True)
        _asyncio.run(_cs._maybe_attach_reground_on_mic(s, st, _loud()))
        assert s.sent == [], flag


def test_one_slot_one_note_reground_takes_the_turn():
    st, s = _st(_seeds.TURN_NOTE_FREETALK), _Rec()
    st.reground_pending, st.reground_reminder = True, "[안내] 재접지 쪽지"
    _asyncio.run(_cs._maybe_attach_reground_on_mic(s, st, _loud()))
    _asyncio.run(_cs._maybe_attach_reground_on_mic(s, st, _loud()))
    assert s.sent == [("[안내] 재접지 쪽지", False)], "재접지가 이 턴을 쓰면 같은 턴에 안내를 또 얹지 않는다"


def test_no_tracker_brief_is_injected_anywhere():
    import inspect
    src = inspect.getsource(_cs)
    assert ".brief()" not in src, "C2 — 추적기 brief() 는 관측·로그 전용"


# --------------------------------------------------------------------------- #
# PM-DEC-726 — 추적기 경로 드릴 상한(같은 목표 문장 반복 · call 2008 t17~t27)
# --------------------------------------------------------------------------- #
from domains.learning.realtime.expression_practice import requested_phrases as _requested


def _practice_state():
    st = _cs._CallState()
    st.expr_items = [{"item_id": 1, "obj": "안녕하세요?"}, {"item_id": 2, "obj": "만나서 반갑습니다"}, {"item_id": 3, "obj": "잘 지냈어요?"}]
    st.expr_practice = ExpressionPractice(st.expr_items)
    st.expr_practice.index = 1
    st.expr_practice.phase = "native"
    st.expr_practice.native_phrase = "반갑습니다"
    return st


def test_requested_phrases_reads_quotes_or_korean_runs_and_skips_short_hints():
    assert _requested("Now try repeating the native version again: 반갑습니다! Say it.") == ["반갑습니다"]
    assert _requested('Tell them, "만나서 반가워요," again.') == ["만나서반가워요"]
    assert _requested("Listen carefully: 반... Now repeat it.") == []          # 힌트 한 글자는 목표 문장이 아니다


def test_call_2008_loop_arms_move_on_on_the_third_miss_and_skips_the_item():
    st = _practice_state()
    turns = [("B", "Now try repeating the native version again: 반갑습니다! Say it."), ("U", "방구 마렵습니다"),
             ("B", "Ugh, are you serious right now? Focus! Try saying 반갑습니다! again."), ("U", "방구가 마렵습니다"),
             ("B", "Your pronunciation is as bad as your manners! Listen carefully: 반... Now repeat it."), ("U", "안구가 너무나 마릅습니디."),
             ("B", "My eyes are drying up from watching you fail! It's 반갑습니다! Say it.")]
    for role, text in turns:
        (_cs._note_expression_drill if role == "B" else _cs._note_drill_user_turn)(st, text)
    assert st.expr_drill_nudge_pending is True, "3번째 불일치에서 «다음 표현으로» 1회"
    assert st.expr_practice.index == 2 and 2 in st.expr_practice.completed, "추적기도 그 항목을 미통과로 넘긴다"
    assert "반갑습니다" in st.expr_rep_nudged


def test_two_misses_are_not_enough_and_a_match_resets():
    st = _practice_state()
    for role, text in [("B", "Say 반갑습니다!"), ("U", "방구"), ("B", "Again: 반갑습니다!"), ("U", "방구가")]:
        (_cs._note_expression_drill if role == "B" else _cs._note_drill_user_turn)(st, text)
    assert st.expr_drill_nudge_pending is False and st.expr_rep_misses == 2
    _cs._note_drill_user_turn(st, "네, 반갑습니다.")                         # 군말이 붙어도 문장이 통째로 있으면 맞음
    assert st.expr_rep_misses == 0 and st.expr_rep_phrase is None and st.expr_drill_nudge_pending is False


def test_repeat_cap_is_off_in_the_quiz_window_and_once_per_phrase():
    st = _practice_state()
    st.expr_quiz_open = True
    for role, text in [("B", "Say 반갑습니다!"), ("U", "a"), ("U", "b"), ("U", "c")]:
        (_cs._note_expression_drill if role == "B" else _cs._note_drill_user_turn)(st, text)
    assert st.expr_drill_nudge_pending is False, "퀴즈 진행 중에는 세지 않는다"
    st2 = _practice_state()
    st2.expr_rep_nudged.add("반갑습니다")
    for role, text in [("B", "Say 반갑습니다!"), ("U", "a"), ("U", "b"), ("U", "c")]:
        (_cs._note_expression_drill if role == "B" else _cs._note_drill_user_turn)(st2, text)
    assert st2.expr_drill_nudge_pending is False, "같은 문장에는 한 번만"


def test_old_path_without_tracker_keeps_its_own_cap():
    st = _cs._CallState()
    st.expr_items = [{"item_id": 1, "obj": "안녕하세요?"}]
    st.expr_drill_focus = 1
    for _ in range(_cs.EXPR_DRILL_MAX_USER_TURNS + 1):
        _cs._note_drill_user_turn(st, "틀린 답")
    assert st.expr_drill_nudge_pending is True and st.expr_rep_misses == 0, "추적기 없는 경로는 종전 합산 상한 · 새 카운터는 안 움직인다"


def test_a_new_question_in_the_native_language_clears_the_old_target_but_a_hint_keeps_it():
    """모의 2007 t26 · 2008 t18 — 새 항목을 영어로만 물으면 지난 목표를 비운다. 첫 어절 힌트는 같은 문장을 계속 시키는 중이다."""
    st = _practice_state()
    for role, text in [("B", "Say 반갑습니다!"), ("U", "방구"), ("B", "Listen carefully: 반... Now repeat it.")]:
        (_cs._note_expression_drill if role == "B" else _cs._note_drill_user_turn)(st, text)
    assert st.expr_rep_phrase == "반갑습니다" and st.expr_rep_misses == 1, "힌트 턴은 목표 유지"
    _cs._note_expression_drill(st, 'Okay, now how would you say "How do you do?" in Korean? Try that one.')
    assert st.expr_rep_phrase is None and st.expr_rep_misses == 0, "새 질문(모국어)은 지난 목표를 비운다"
    _cs._note_drill_user_turn(st, "어떻게 지내셨어요?")
    assert st.expr_rep_misses == 0


# --------------------------------------------------------------------------- #
# PM-DEC-752 S1 — 첫 어절 힌트 「어디에서...」 뒤 같은 문장 전체가 나오면 불일치 수를 이어 센다(call 2020 B21~B29)
# --------------------------------------------------------------------------- #
def test_call_2020_hint_then_full_sentence_keeps_counting_and_arms_move_on():
    st = _practice_state()
    turns = [
        ("B", 'Ugh, already forgetting? Try starting with "어디에서..." What do you say after that?'), ("U", "어디에도 그대 가사"),
        ("B", 'Hah! What was that terrible singing? Just say "어디에서 왔어요?" Now repeat that.'), ("U", ""),
        ("B", "어디에서 왔어요? That's what you should have said. Now, ask someone else where they're from."), ("U", "다가갈수록 미워지니까"),
        ("B", 'Stop with the awful singing! Just say "어디에서 왔어요?" again. Now, repeat that.'), ("U", "Audio so what's up?"),
    ]
    for role, text in turns:
        (_cs._note_expression_drill if role == "B" else _cs._note_drill_user_turn)(st, text)
    assert st.expr_drill_nudge_pending is True, "힌트 뒤 오답 1 + 노래 1 + 엉뚱한 답 1 = 3회째에 «다음 표현으로»"
    assert "어디에서왔어요" in st.expr_rep_nudged


def test_a_different_sentence_still_resets_the_miss_count():
    st = _practice_state()
    for role, text in [("B", 'Say "어디에서 왔어요?"'), ("U", "몰라"), ("U", "몰라요"), ("B", 'Now say "이름이 뭐예요?"')]:
        (_cs._note_expression_drill if role == "B" else _cs._note_drill_user_turn)(st, text)
    assert st.expr_rep_phrase == "이름이뭐예요" and st.expr_rep_misses == 0 and st.expr_drill_nudge_pending is False
