"""2026-09-14 1차 묶음(사장님 «진행해», 근거 통화 1601·1602 ja) — 대본 C1·C2·C3 · 퀴즈 창 안전장치 C4 · 재개 쪽지 C5·C6.

ko 잠금 대본 바이트 불변은 tests/test_prompt_locked_hash.py(expression.procedure 5c0c6ac8e65fb352)가 지킨다 — 여기서는 ja 에 줄이 들어갔는지·ko 에 안 들어갔는지 본다.
"""
# ⛔ 2026-10-10 퀴즈 상태기계·판정 사이드카 삭제로 **떼어낸 시험**(되살리려면 커밋 c222911):
#   · test_cue_arms_as_soon_as_three_unquizzed_items_gather_even_if_the_learner_says_two_at_once
#   · test_empty_learner_turns_do_not_count_toward_the_cue_settle_wait
#   · test_empty_learner_turns_do_not_count_toward_the_quiz_window_cap
#   · test_quiz_cue_hold_log_is_emitted_only_when_the_reason_changes
#   · test_quiz_set_is_in_item_order_not_covered_order
#   · test_quiz_window_counter_resets_per_window_and_ignores_closed_state
#   · test_quiz_window_is_force_closed_after_six_learner_turns_and_the_next_cue_can_arm
#   · test_tail_cue_is_kept_below_the_group_size
#   · test_three_six_nine_groups_are_unchanged
from __future__ import annotations

import pytest

import domains.learning.realtime.call_session as cs
from core.prompts.locked import expression as lex
from core.prompts.locked import reground, seeds
from core.prompts.expression import build_expression_instruction


def _instr(language: str, target: str) -> str:
    return build_expression_instruction(
        role="선생님", personality="다정함", level_profile="초급", locale="ko" if language == "ja" else "en", interests=[], name="Sam",
        items=[{"item_id": 1, "obj": "こんにちは" if language == "ja" else "안녕하세요", "des": "안녕하세요(낮)" if language == "ja" else "hello", "ex": None}],
        quiz_group=3, target_language=target, close_tag="[통화종료:ab12]", model_family="3.1", language=language,
    )


# --------------------------------------------------------------------------- #
# C1·C2 — 비ko 전용 드릴 줄 · ko 무변화
# --------------------------------------------------------------------------- #
def test_target_script_line_is_non_ko_only_and_ask_first_is_common_to_every_language():
    """C1(비ko 전용 표기 줄)은 그대로 · ⭐ 15차(2026-09-19, 사장님 지시 — 1657 ko t13): «먼저 물어라» 는 **전 언어 공통**이다."""
    ja = _instr("ja", "일본어")
    assert "일본어 낱말·문장은 언제나 일본어 문자로 말하고 적어라 — 학습자 모국어 문자로 음차해 적거나 읽지 마라." in ja, "C1"
    ask = "항목마다 **먼저 물어보고** 학습자가 시도한 뒤에만 정답을 공개해라(못 하면 최대 3번)"
    assert ja.count(ask) == 1, "ja 는 정확히 1회 — 공통 자리로 옮기며 비ko 목록에서 뺐다(중복 0)"
    ko = _instr("ko", "한국어")
    assert "음차해 적거나 읽지 마라" not in ko, "표기 줄은 여전히 비ko 전용"
    assert ko.count(ask) == 1, "15차 — ko 도 이제 «먼저 물어라» 를 받는다(1657 t13 재발 방지)"
    assert lex.DRILL_EXTRA_LINES_BY_LANGUAGE["ko"] == ()
    assert lex.DRILL_EXTRA_LINES_DEFAULT == (lex.DRILL_TARGET_SCRIPT_LINE,), "비ko 전용 목록에는 표기 줄만 남는다"
    # 자리: drill_intro 바로 뒤 — ja 는 종전 순서 그대로(표기 → 먼저 물어라), ko 는 drill_intro 바로 뒤
    proc_ja = lex.procedure(drill_intro="- 드릴", target="일본어", locale_label="한국어", language="ja").splitlines()
    assert proc_ja[1] == "- 드릴" and proc_ja[2].startswith("- 일본어 낱말·문장은") and proc_ja[3].startswith("- 항목마다") and proc_ja[4] == lex.DRILL_REVEAL_LINE
    proc_ko = lex.procedure(drill_intro="- 드릴", target="한국어", locale_label="영어(English)").splitlines()
    assert proc_ko[1] == "- 드릴" and proc_ko[2].startswith("- 항목마다") and proc_ko[3] == lex.DRILL_REVEAL_LINE


# --------------------------------------------------------------------------- #
# C3 — 편집 대본: 목록을 다 돌아도 끝내지 마라(ko·ja 공통, 금지어 없이)
# --------------------------------------------------------------------------- #
def test_expression_prompt_forbids_self_ending_after_the_list():
    line = "목록을 다 돌아도 네가 통화를 끝내지 마라 — 아직 해내지 못한 항목을 다시 시키고, 남는 시간은 배운 표현을 바꿔 가며 계속 이어가라. 끝내는 때는 서버가 알린다."
    assert line in _instr("ko", "한국어") and line in _instr("ja", "일본어")
    for banned in ("작별", "종료", "마지막", "마무리", "정리", "여기까지", "퀴즈"):
        assert banned not in line


# --------------------------------------------------------------------------- #
# C4 — 퀴즈 창 안전장치: 학습자 턴 6 이 지나도 안 닫히면 강제 닫힘 + 다음 큐 arm 가능
# --------------------------------------------------------------------------- #
ITEMS = [{"item_id": 10 + i, "obj": s, "des": "d", "ex": None} for i, s in enumerate(
    ["이거 얼마예요?", "잘 부탁드립니다", "도와주세요", "처음 뵙겠습니다", "네", "감사합니다", "안녕하세요", "또 봐요", "미안해요"], 1)]


def _state() -> cs._CallState:
    st = cs._CallState()
    st.expr_items = list(ITEMS)
    st.reground_items = [i["obj"] for i in ITEMS]
    st.expr_ctx = None            # 폴백 사이드카 없이(닫힘 자체만 본다)
    st.reground_persona = ("선생님", "다정함")
    return st


def _beaver(st, text):
    st.cur_beaver_text = [text]
    cs._flush_beaver_segment(st)


def _user(st, text):
    st.cur_user_text = [text]
    cs._flush_user_segment(st)


def test_expression_resume_note_has_the_required_sections_and_stays_short():
    mats = dict(
        drilled=["こんにちは", "おはようございます", "こんばんは", "さようなら", "またね", "お元気ですか", "いってきます", "いらっしゃいませ",
                 "ありがとうございます", "どうも", "どういたしまして", "すみません", "ごめんなさい", "大丈夫です", "はい", "いただきます", "おやすみなさい", "はじめまして"],
        passed=["こんにちは", "おはようございます", "こんばんは", "さようなら", "またね", "お元気ですか", "いってきます", "ありがとうございます", "どうも", "はい"],
        failed=["いらっしゃいませ", "どういたしまして"],
        recent=[("beaver", "오케이, 그것도 맞았어! 잘하고 있네. 그럼 이번에는 친구랑 헤어질 때, 또 봐라고 하잖아? 그걸 일본어로는 어떻게 말하게? 얼른 던져봐! " * 2),
                ("user", "맞다네"), ("beaver", "오케이, 그것도 맞았어! 잘하고 있네." * 5), ("user", "また ね 。")],
    )
    for fn in (seeds.seed_expression_resume, seeds.brief_expression_silent_resume):
        note = fn("일본어", **mats)
        assert len(note) <= seeds.RESUME_NOTE_MAX_CHARS == 900, (fn.__name__, len(note))
        assert note.startswith("[통화 이어감]")
        assert "이미 한 것: 드릴 18개" in note and "퀴즈 통과 (" in note and "다시 가르치지 마라" in note
        assert "오답이었던 것(いらっしゃいませ, どういたしまして)은 한 번 더 시켜 보고" in note
        assert "바로 전 대화:" in note and "학습자 «また ね 。»" in note and "**짧게(2문장)**" in note
        assert "«왔냐?»류 시작말도 하지 마라" in note and "인사하지 말고" in note and "(일본어 학습을 계속한다.)" in note
        assert "소리 내어 읽지 말고" in note
    seed = seeds.seed_expression_resume("일본어", **mats)
    silent = seeds.brief_expression_silent_resume("일본어", **mats)
    assert "지금 바로 이어가라" in seed and "먼저 말을 꺼내지 말고 기다렸다가" not in seed
    assert "먼저 말을 꺼내지 말고 기다렸다가" in silent and "지금 바로 이어가라" not in silent
    # 재료 없이(옛 호출·조회 실패) 도 형식이 선다
    bare = seeds.seed_expression_resume("한국어")
    assert "드릴 0개 · 퀴즈 통과 없음" in bare and "남은 것 = [오늘의 표현] 목록 그대로다 — 새로 가르쳐라." in bare and "바로 전 대화" not in bare
    # C5 silent 일반 브리프 마지막 줄
    assert "«왔냐?»·«어, 왔어?» 같은 시작말도 쓰지 말고" in reground.RESUME_SILENT_FIRST_ACTION
    # 조각1 대본(선톡 시드) 무변경 — 재개 쪽지가 새지 않았다
    from core.prompts.expression import seed_expression_opening
    assert "[통화 시작]" in seed_expression_opening("한국어") and "[통화 이어감]" not in seed_expression_opening("한국어")


# --------------------------------------------------------------------------- #
# E — [문형] 항목은 같은 문형의 다른 올바른 문장도 정답(ko·ja 공통, has_grammar 일 때만)
# --------------------------------------------------------------------------- #
def test_grammar_items_accept_other_correct_sentences_of_the_same_pattern():
    for lang, target, loc in (("ko", "한국어", "영어(English)"), ("ja", "일본어", "한국어")):
        with_gr = lex.procedure(drill_intro="- 드릴", target=target, locale_label=loc, has_grammar=True, language=lang).splitlines()
        i = with_gr.index(lex.DRILL_GRAMMAR_LINE)
        assert with_gr[i + 1] == lex.DRILL_GRAMMAR_ALT_LINE, "DRILL_GRAMMAR_LINE 바로 뒤"
        assert "같은 문형으로 만든 다른 올바른 문장**도 정답" in lex.DRILL_GRAMMAR_ALT_LINE and "문형 자체가 틀렸을 때만 교정" in lex.DRILL_GRAMMAR_ALT_LINE
        without = lex.procedure(drill_intro="- 드릴", target=target, locale_label=loc, has_grammar=False, language=lang)
        assert lex.DRILL_GRAMMAR_ALT_LINE not in without and lex.DRILL_GRAMMAR_LINE not in without, "무문법 차시 무변경"


# --------------------------------------------------------------------------- #
# ④ (2026-09-14) — (a) 새 표현 첫 질문 틀(편집 대본) · (b) 큐 «보류» 로그는 사유가 바뀔 때만
# --------------------------------------------------------------------------- #
def test_new_item_first_ask_frame_is_in_the_drill_intro_for_all_languages():
    line = "처음 묻는 새 표현이면 새 표현임을 먼저 알리고, 알면 말해 보고 모르면 알려 주겠다는 틀로 물어라 — 배운 적 없는 것을 맞춰 보라고 몰아세우지 마라."
    assert line in _instr("ko", "한국어") and line in _instr("ja", "일본어")
    for banned in ("작별", "종료", "마지막", "마무리", "정리", "여기까지", "퀴즈", "테스트"):
        assert banned not in line


def _empty_user(st):
    st.cur_user_pcm = bytearray(b"\x00\x00" * 160)     # 소리는 왔는데 전사가 비었다
    st.cur_user_text = []
    cs._flush_user_segment(st)


def _close_quiz_now(st):
    st.expr_quiz_cue_pending = None
    st.expr_quiz_awaiting_open = True
    cs._expression_quiz_open_on_beaver_turn(st)
    cs._close_expression_quiz(st, why="시험")


def test_resume_note_stats_report_the_real_length_and_the_shrink_step():
    short = dict(drilled=["물"], passed=["물"], failed=[], recent=[("beaver", "물은 water"), ("user", "물")])
    st = seeds.expression_resume_note_stats("한국어", silent=False, **short)
    assert st["len"] == len(seeds.seed_expression_resume("한국어", **short)) and st["step"] == 0 and st["recent_n"] == 4 and st["recent_available"] == 2
    long_mats = dict(drilled=["표현%02d 이것은 긴 표면형" % i for i in range(18)], passed=["표현%02d 이것은 긴 표면형" % i for i in range(12)],
                     failed=["표현%02d 이것은 긴 표면형" % i for i in range(12, 18)],
                     recent=[("beaver", "가" * 120), ("user", "나" * 120), ("beaver", "다" * 120), ("user", "라" * 120)])
    for silent, fn in ((False, seeds.seed_expression_resume), (True, seeds.brief_expression_silent_resume)):
        s = seeds.expression_resume_note_stats("한국어", silent=silent, **long_mats)
        assert s["len"] == len(fn("한국어", **long_mats)) <= s["max"] == 900, "계측 길이 = 실제 쪽지 길이"
        assert s["step"] > 0 and s["lists"] == (18, 12, 6), s
