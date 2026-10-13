"""PM-DEC-626 네 번째 배포 — 진행 추적기 따라가기(P12-a·c) · 숙제 현지인 단계 없음(P12-b) · 남은 항목 번호 빼기(P5) ·
빈칸 라벨(P5-b) · 저장 전사 공백(P11-a) · 번역 대소문자(번역 지시문). 외부 모델/DB/네트워크 0."""
from core import text_translate
from core.prompts.homework import HOMEWORK_NO_NATIVE_LINE, build_homework_expression_instruction
from core.prompts.locked.expression import render_item
from core.prompts.locked.freetalk import fill_slots
from domains.learning.realtime import call_session as cs
from domains.learning.realtime.expression_practice import ExpressionPractice

ITEMS = [
    {"obj": "어제 비가 많이 왔어요", "des": "It rained a lot yesterday", "ex": None},
    {"obj": "사과를 하나 샀어요", "des": "I bought an apple", "ex": None},
    {"obj": "이 가방 얼마예요?", "des": "How much is this bag?", "ex": None},
    {"obj": "화장실이 어디예요?", "des": "Where is the restroom?", "ex": None},
]


# ── P12-a·c 비버가 먼저 넘어가면 따라간다 ───────────────────────────────
def test_tracker_follows_beaver_to_a_later_item():
    p = ExpressionPractice(ITEMS)
    p.observe_beaver('좋아요. 다음은 "이 가방 얼마예요?" 따라 해 보세요.')
    assert p.index == 2 and p.phase == "original"
    assert "어제 비가 많이 왔어요" not in p.brief(), "끝난(지나간) 항목을 다시 내라고 하지 않는다"


def test_tracker_ignores_a_list_of_several_later_items():
    p = ExpressionPractice(ITEMS)
    p.observe_beaver("오늘은 사과를 하나 샀어요, 이 가방 얼마예요?, 화장실이 어디예요? 를 배워요.")
    assert p.index == 0, "여러 뒤 항목을 한꺼번에 말하면(목록 소개) 따라가지 않는다"


def test_short_quoted_particle_is_not_a_native_phrase():
    p = ExpressionPractice(ITEMS)
    p.observe_beaver("어제 비가 많이 왔어요 를 말해 보세요.")
    p.observe_user("어제 비가 많이 왔어요")
    assert p.phase == "native_intro"
    p.observe_beaver('현지인은 "어제 비 엄청 왔어" 라고 해요. 여기서 "은" 은 조사예요.')
    assert p.phase == "native" and p.native_phrase == "어제 비 엄청 왔어"


# ── P12-b 숙제는 현지인 단계 없음 ─────────────────────────────────────
def test_homework_tracker_skips_native_step():
    p = ExpressionPractice(ITEMS, native_step=False)
    p.observe_beaver("어제 비가 많이 왔어요 를 말해 보세요.")
    p.observe_user("어제 비가 많이 왔어요")
    assert p.index == 1 and p.phase == "introduce"
    assert "현지인 표현은 만들지 않는다" in p.brief()
    assert "현지인 문장 소개" not in p.brief()


def test_regular_expression_keeps_native_step():
    p = ExpressionPractice(ITEMS)
    p.observe_beaver("어제 비가 많이 왔어요 를 말해 보세요.")
    p.observe_user("어제 비가 많이 왔어요")
    assert p.phase == "native_intro"


def test_homework_expression_instruction_says_no_native_step():
    hw = {"assignment_id": 8, "source": "manual", "language": "ko", "activities": ["conversation"],
          "grammar": [], "goals": [{"reference": "assignment_item", "id": 1, "surface": "안녕하세요",
                                     "meaning": "hello", "example": None, "conversation": True,
                                     "example_meaning": None}]}
    text = build_homework_expression_instruction(
        role="친구", personality="밝음", level_profile="", locale="en", name=None, homework=hw, quiz_group=3,
    )
    assert text.rstrip().endswith(HOMEWORK_NO_NATIVE_LINE)


# ── P5 남은 항목 줄에 번호 없음 ──────────────────────────────────────
def test_remaining_rows_have_no_numbers():
    class _S:
        reground_items = [it["obj"] for it in ITEMS]
        expr_items = ITEMS
        covered_nums = [1]
    rows = cs._expr_remaining_rows(_S())
    assert rows[0] == "I bought an apple = 사과를 하나 샀어요"
    assert not any(r[:1].isdigit() for r in rows)


# ── P5-b 빈칸 라벨 ───────────────────────────────────────────────────
def test_slot_labels():
    assert fill_slots("저는 ◯◯ 회원 아니에요") == "저는 (가게 이름) 회원 아니에요"
    assert fill_slots("저는 ◯◯이에요") == "저는 (이름)이에요"
    assert fill_slots("저는 ◯◯ 사람이에요") == "저는 (나라) 사람이에요"
    assert fill_slots("◯◯ 주세요") == "(빈칸) 주세요"


def test_expression_list_fills_slots_but_keeps_plain_items():
    assert render_item(1, {"obj": "저는 ◯◯이에요", "des": "I'm ___."}) == "1. 저는 (이름)이에요 — 뜻: I'm ___."
    assert render_item(2, {"obj": "안녕하세요", "des": "hello"}) == "2. 안녕하세요 — 뜻: hello"


# ── P11-a 저장 전사 공백 ─────────────────────────────────────────────
def test_glued_responses_get_a_space():
    assert cs._join_beaver_text(["let's move on to the next one.", "Well, that was good."]) == \
        "let's move on to the next one. Well, that was good."
    assert cs._join_beaver_text(["Hel", "lo", " there."]) == "Hello there."
    assert cs._join_beaver_text(["좋아요.", " 다음"]) == "좋아요. 다음"


# ── 번역 지시문 대소문자 ─────────────────────────────────────────────
def test_translate_instruction_forbids_title_case():
    assert "do not capitalize every word like a title" in text_translate._SYSTEM
