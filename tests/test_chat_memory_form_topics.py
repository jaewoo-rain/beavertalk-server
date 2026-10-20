"""PM-DEC-889 — call flow 71 요청서: 대화 형식 화제 건너뛰기 · 기억 인사 질문 하나 · 자유대화 전용 넛지."""
from core.prompts import chat as chat_prompt
from core.prompts.locked import seeds


def test_form_topic_skipped_then_real_topic_used():
    seed = chat_prompt.seed_chat_opening("한국어", {"topics": ["일상 대화", "안부 인사", "좋아하는 음식"]})
    assert "좋아하는 음식" in seed and "안부 인사" not in seed


def test_only_form_topics_falls_back_to_facts_then_empty():
    assert "고양이를 키움" in chat_prompt.seed_chat_opening("한국어", {"topics": ["안부 인사"], "facts": ["고양이를 키움"]})
    assert chat_prompt.seed_chat_opening("한국어", {"topics": ["안부 인사", "자기소개"]}) == ""


def test_opening_asks_one_question():
    seed = chat_prompt.seed_chat_opening("한국어", {"topics": ["등산"]})
    assert "물음표는 한 번뿐" in seed and "질문 한 문장만" in seed


def test_chat_nudges_one_question():
    assert "물음표는 한 번뿐" in seeds.NUDGE_SEED_1_CHAT
    assert "다른 질문은 붙이지 않는다" in seeds.NUDGE_SEED_2_CHAT
    assert "거기 있어" not in seeds.NUDGE_SEED_2_CHAT
