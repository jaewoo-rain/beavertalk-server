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
