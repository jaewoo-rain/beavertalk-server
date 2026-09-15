"""2026-09-14 1차 묶음(사장님 «진행해», 근거 통화 1601·1602 ja) — 대본 C1·C2·C3 · 퀴즈 창 안전장치 C4 · 재개 쪽지 C5·C6.

ko 잠금 대본 바이트 불변은 tests/test_prompt_locked_hash.py(expression.procedure 5c0c6ac8e65fb352)가 지킨다 — 여기서는 ja 에 줄이 들어갔는지·ko 에 안 들어갔는지 본다.
"""
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
def test_non_ko_drill_gets_script_and_ask_first_lines_but_ko_does_not():
    ja = _instr("ja", "일본어")
    assert "일본어 낱말·문장은 언제나 일본어 문자로 말하고 적어라 — 학습자 모국어 문자로 음차해 적거나 읽지 마라." in ja, "C1"
    assert "항목마다 **먼저 물어보고** 학습자가 시도한 뒤에만 정답을 공개해라(못 하면 최대 3번)" in ja, "C2"
    ko = _instr("ko", "한국어")
    assert "음차해 적거나 읽지 마라" not in ko and "먼저 물어보고** 학습자가 시도한 뒤에만" not in ko, "ko 대본 무변화(해시 시험이 바이트를 지킨다)"
    assert lex.DRILL_EXTRA_LINES_BY_LANGUAGE["ko"] == ()
    # 두 줄은 drill_intro 바로 뒤(먼저 묻는 규율이 공개 규율보다 앞에)
    proc = lex.procedure(drill_intro="- 드릴", target="일본어", locale_label="한국어", language="ja").splitlines()
    assert proc[1] == "- 드릴" and proc[2].startswith("- 일본어 낱말·문장은") and proc[3].startswith("- 항목마다") and proc[4] == lex.DRILL_REVEAL_LINE


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


def test_quiz_window_is_force_closed_after_six_learner_turns_and_the_next_cue_can_arm():
    st = _state()
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"도와주세요"'):
        _beaver(st, t)
    assert st.expr_quiz_cue_pending is not None, "3개 covered → 큐 1 대기"
    st.expr_quiz_cue_pending = None
    st.expr_quiz_awaiting_open = True
    cs._expression_quiz_open_on_beaver_turn(st)
    assert st.expr_quiz_open is True and st.expr_quiz_seq == 1
    # 1601: 비버가 닫힘 트리거(다음 항목 소개) 없이 같은 문항을 되풀이한다 — 학습자 턴만 쌓인다
    for i in range(5):
        _beaver(st, "How do you say it? Try again.")
        _user(st, "음... 모르겠어요 %d" % i)
        assert st.expr_quiz_open is True, "5턴까지는 열려 있다"
    _beaver(st, "One more time?")
    _user(st, "이거 얼마예요?")                 # 6번째 학습자 턴 → 강제 닫힘(이 발화도 창 안에 든다 → 서버 판정 통과)
    assert st.expr_quiz_open is False and st.expr_quiz_open_user_turns == 6
    assert 11 in st.expr_quiz_pass, "창 안 마지막 발화가 판정에 들어갔다"
    assert cs.EXPR_QUIZ_OPEN_MAX_USER_TURNS == 6
    # 다음 큐가 열린다 — 3개 더 covered 되면 seq 2
    for t in ('"처음 뵙겠습니다"', '"네"', '"감사합니다"'):
        _beaver(st, t)
    assert st.expr_quiz_seq == 2 and st.expr_quiz_cue_pending is not None, "1601 에서는 첫 창이 끝까지 열려 다음 큐 0 이었다"


def test_quiz_window_counter_resets_per_window_and_ignores_closed_state():
    st = _state()
    _user(st, "안녕")                            # 창 없음 → 세지 않는다
    assert st.expr_quiz_open_user_turns == 0
    st.expr_quiz_awaiting_open = True
    cs._expression_quiz_open_on_beaver_turn(st)
    _user(st, "하나")
    assert st.expr_quiz_open_user_turns == 1
    st.expr_quiz_open = False
    st.expr_quiz_awaiting_open = True
    cs._expression_quiz_open_on_beaver_turn(st)
    assert st.expr_quiz_open_user_turns == 0, "창마다 리셋"


# --------------------------------------------------------------------------- #
# C5·C6 — 재개 쪽지: 형식·재료·길이 상한·«왔냐»류 금지 · 조각1 대본 무변경
# --------------------------------------------------------------------------- #
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


@pytest.mark.asyncio
async def test_quiz_cue_hold_log_is_emitted_only_when_the_reason_changes(caplog):
    import logging

    class _Sess:
        async def send_reground(self, text, *, turn_complete=True):
            pass

    st = _state()
    st.expr_quiz_cue_pending = "[큐]"
    st.expr_quiz_prev_num = 1                        # 보류 항목 — 학습자 입에서 안 나왔다
    st.expr_quiz_cue_user_turns = 0
    caplog.set_level(logging.INFO, logger="domains.learning.realtime.call_session")
    for _ in range(15):                              # 1604: 마이크 프레임마다 15회
        await cs._attach_quiz_cue(_Sess(), st, "마이크")
    holds = [r for r in caplog.records if "보류:" in r.getMessage()]
    assert len(holds) == 1, [r.getMessage() for r in holds]
    st.expr_quiz_cue_user_turns = 1                  # 사유가 바뀐다(학습자 턴 1/3) → 1줄 더
    await cs._attach_quiz_cue(_Sess(), st, "마이크")
    await cs._attach_quiz_cue(_Sess(), st, "마이크")
    holds = [r for r in caplog.records if "보류:" in r.getMessage()]
    assert len(holds) == 2 and "1/3" in holds[-1].getMessage()


# --------------------------------------------------------------------------- #
# P1 (2026-09-15, 1611 t0·t28·t40) — 전사 빈 턴은 창 상한·큐 정리 대기를 세지 않는다
# --------------------------------------------------------------------------- #
def _empty_user(st):
    st.cur_user_pcm = bytearray(b"\x00\x00" * 160)     # 소리는 왔는데 전사가 비었다
    st.cur_user_text = []
    cs._flush_user_segment(st)


def test_empty_learner_turns_do_not_count_toward_the_quiz_window_cap():
    st = _state()
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"도와주세요"'):
        _beaver(st, t)
    st.expr_quiz_cue_pending = None
    st.expr_quiz_awaiting_open = True
    cs._expression_quiz_open_on_beaver_turn(st)
    n_seg = len(st.segments)
    for _ in range(6):
        _empty_user(st)
    assert st.expr_quiz_open is True and st.expr_quiz_open_user_turns == 0, "무음 턴 6회 → 강제 닫힘 0"
    assert len(st.segments) == n_seg + 6, "세그먼트·turn_index 는 종전대로 저장된다"
    for i in range(6):
        _user(st, "음 %d" % i)
    assert st.expr_quiz_open is False, "전사 있는 턴 6회면 종전대로 닫힌다"


def test_empty_learner_turns_do_not_count_toward_the_cue_settle_wait():
    st = _state()
    for t in ('"이거 얼마예요?"', '"잘 부탁드립니다"', '"도와주세요"'):
        _beaver(st, t)
    assert st.expr_quiz_cue_pending is not None and st.expr_quiz_cue_user_turns == 0
    for _ in range(4):
        _empty_user(st)
    assert st.expr_quiz_cue_user_turns == 0, "무음 턴은 정리 대기 카운트 0"
    _user(st, "모르겠어요")
    assert st.expr_quiz_cue_user_turns == 1


# --------------------------------------------------------------------------- #
# P2 (2026-09-15, 1607) — 큐 조건 = 미출제 g개 모이면(covered 총량 기준 폐기) · 종전 3·6·9 동일 · 꼬리 유지
# --------------------------------------------------------------------------- #
def _close_quiz_now(st):
    st.expr_quiz_cue_pending = None
    st.expr_quiz_awaiting_open = True
    cs._expression_quiz_open_on_beaver_turn(st)
    cs._close_expression_quiz(st, why="시험")


def test_cue_arms_as_soon_as_three_unquizzed_items_gather_even_if_the_learner_says_two_at_once():
    st = _state()
    _beaver(st, '"이거 얼마예요?"')
    assert st.expr_quiz_cue_pending is None
    _user(st, "잘 부탁드립니다. 도와주세요.")          # 한 턴에 2개 → 미출제 3개
    assert st.expr_quiz_cue_pending is not None and st.expr_quiz_seq == 1 and sorted(st.expr_quiz_set) == [1, 2, 3]
    _close_quiz_now(st)
    # 퀴즈가 닫힌 뒤 한 번에 4개가 covered 되면 3개로 큐 — 남은 1개는 다음 묶음
    _user(st, "처음 뵙겠습니다. 네. 감사합니다. 안녕하세요.")
    assert st.expr_quiz_seq == 2 and sorted(st.expr_quiz_set) == [4, 5, 6]


def test_three_six_nine_groups_are_unchanged():
    st = _state()
    seqs = []
    for i, it in enumerate(ITEMS, 1):
        _beaver(st, '"%s"' % it["obj"])
        if st.expr_quiz_cue_pending is not None:
            seqs.append((i, st.expr_quiz_seq, list(st.expr_quiz_set)))
            _close_quiz_now(st)
    assert seqs == [(3, 1, [1, 2, 3]), (6, 2, [4, 5, 6]), (9, 3, [7, 8, 9])]


def test_tail_cue_is_kept_below_the_group_size():
    st = _state()
    st.expr_items = list(ITEMS[:5])
    st.reground_items = [i["obj"] for i in ITEMS[:5]]
    for it in ITEMS[:3]:
        _beaver(st, '"%s"' % it["obj"])
    _close_quiz_now(st)
    _beaver(st, '"%s"' % ITEMS[3]["obj"])
    assert st.expr_quiz_cue_pending is None, "미출제 1개 — 아직"
    _beaver(st, '"%s"' % ITEMS[4]["obj"])
    assert st.expr_quiz_cue_pending is not None and sorted(st.expr_quiz_set) == [4, 5], "목록 끝이면 남은 2개로 꼬리 큐"
