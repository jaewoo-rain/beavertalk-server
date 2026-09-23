"""C7(2026-09-23) — core/prompts/chat.py 순수 조립 시험(LLM 생성 0, DB 없음).

시험 목록(문서 그대로 + bt-back 추가):
    - chat 지시문에 차시 블록 없음
    - 기억 있으면 [기억] 블록·없으면 생략
    - 관심사 없으면 생략
    - QA C7-③: 기억이 빈약(사실 0·화제 0)하면 "아는 척" 시드를 안 쓴다(D8 오프닝 폴백)
"""

from __future__ import annotations

from core.prompts import chat as chat_prompt
from core.prompts.freetalk import seed_freetalk_opening

_ARGS = dict(
    role="선생님", personality="다정함", level_profile="초급", locale="en",
    name="학습자", target_language="한국어", close_tag="[[END:tag]]",
)


def test_chat_instruction_has_no_lesson_block():
    """차시 프리토킹의 "[이번 차시" 마커가 chat 지시문에 없어야 한다(D8, lesson=None)."""
    instruction = chat_prompt.build_chat_instruction(interests=[], **_ARGS)
    assert "[이번 차시" not in instruction
    assert "역할극" not in instruction  # 차시 프리토킹 전용 문구


def test_interests_block_is_omitted_when_empty():
    instruction = chat_prompt.build_chat_instruction(interests=[], **_ARGS)
    assert "[관심사]" not in instruction


def test_interests_block_is_included_when_present():
    instruction = chat_prompt.build_chat_instruction(interests=["요리", "여행"], **_ARGS)
    assert "[관심사] 요리, 여행" in instruction


def test_memory_block_is_omitted_when_there_is_no_memory():
    instruction = chat_prompt.build_chat_instruction(interests=[], memory=None, **_ARGS)
    assert "[기억" not in instruction


def test_memory_block_is_omitted_when_memory_is_empty():
    empty_memory = {"summary": "", "topics": [], "facts": [], "interests": [], "next_topics": []}
    instruction = chat_prompt.build_chat_instruction(interests=[], memory=empty_memory, **_ARGS)
    assert "[기억" not in instruction


def test_memory_block_is_included_when_memory_has_content():
    memory = {"summary": "요리 얘기를 했다.", "topics": ["요리"], "facts": ["채식주의자"],
              "interests": [], "next_topics": ["여행 이야기"]}
    instruction = chat_prompt.build_chat_instruction(interests=[], memory=memory, **_ARGS)
    assert "[기억" in instruction
    assert "요리 얘기를 했다" in instruction
    assert "채식주의자" in instruction
    assert "여행 이야기" in instruction


# --------------------------------------------------------------------------- #
# QA C7-③ — "아는 척" 시드는 기억이 있을 때만
# --------------------------------------------------------------------------- #
def test_seed_chat_opening_is_empty_without_memory():
    assert chat_prompt.seed_chat_opening("한국어", None) == ""


def test_seed_chat_opening_is_empty_when_memory_is_sparse():
    """사실 0·화제 0 이면 빈약하다 — summary 만 있어도 "아는 척" 을 쓰지 않는다."""
    sparse = {"summary": "짧은 통화였다.", "topics": [], "facts": [], "interests": [], "next_topics": []}
    assert chat_prompt.memory_is_substantial(sparse) is False
    assert chat_prompt.seed_chat_opening("한국어", sparse) == ""


def test_seed_chat_opening_uses_memory_when_substantial():
    memory = {"summary": "", "topics": ["요리"], "facts": [], "interests": [], "next_topics": []}
    assert chat_prompt.memory_is_substantial(memory) is True
    seed = chat_prompt.seed_chat_opening("한국어", memory)
    assert seed != ""
    assert "요리" in seed


def test_the_caller_contract_falls_back_to_the_d8_opening_when_seed_is_empty():
    """실제 호출부 패턴 확인: `seed_chat_opening(...) or seed_freetalk_opening(...)`."""
    seed = chat_prompt.seed_chat_opening("한국어", None) or seed_freetalk_opening("한국어")
    assert seed == seed_freetalk_opening("한국어")
