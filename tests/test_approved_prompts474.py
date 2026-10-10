"""승인 본문과 실제 복창/재개/퀴즈 배선. 외부 모델·실제 DB 사용 없음."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.persona_prompt import build_leveltest_instruction, seed_leveltest_opening
from core.prompts import editable_loader as el
from core.prompts.chat import build_chat_instruction, seed_chat_opening
from core.prompts.expression import build_expression_instruction
from core.prompts.freetalk import build_freetalk_instruction
from core.prompts.locked.seeds import seed_expression_resume, brief_expression_silent_resume
from domains.learning.realtime.expression_practice import ExpressionPractice
from domains.learning.realtime import call_session as cs

ITEMS = [
    {'item_id': 10, 'obj': '감사합니다', 'des': 'Thank you', 'native': 'Thank you'},
    {'item_id': 11, 'obj': '안녕하세요', 'des': 'Hello'},
]


def practice():
    return ExpressionPractice(ITEMS)


def native_pending(p):
    p.observe_beaver('Repeat: "감사합니다"')
    p.observe_user('감사합니다.')
    assert p.phase == 'native_intro'
    p.observe_beaver('Naturally: "정말 고마워요". Repeat it.')
    assert p.phase == 'native'


def state():
    s = cs._CallState()
    s.expr_items = ITEMS
    s.reground_items = [i['obj'] for i in ITEMS]
    s.expr_practice = practice()
    return s


@pytest.mark.parametrize('name,key', [('leveltest','intro'), ('expression','persona_intro'), ('freetalk','persona_intro_lesson'), ('freetalk','persona_intro_old')])
def test_approved_body_is_kept_in_full(name,key):
    text = el.section(name,key)
    approved = json.loads((Path(__file__).parent/'fixtures/approved_call_prompts474.json').read_text(encoding='utf-8'))
    assert text == approved[name + ':' + key]
    # 2026-10-10 사고 방어 패치 G1 — 대본에서 종료 개념을 꺼내지 않는다(README 원칙 5 · call 706·782·852·870)
    assert '종료' not in text and '계속 이어간다' in text
    assert el.validate(name,el.load(name),el.load(name)) == []


def test_actual_level_prompt_contains_db_material_and_approved_language():
    out = build_leveltest_instruction(role='role',personality='personality',locale='en',interests=[],grammar_catalog=[{'item_id':321,'level_no':3,'surface':'-고 싶다'}])
    assert '321 / L3 / -고 싶다' in out
    assert '구사·부분 구사·미확인' in out
    assert '네 말(인사·질문·리액션)은 전부' not in out
    assert '자기소개' in seed_leveltest_opening()


def test_expression_assembly_removes_old_teaching_and_retry_rules():
    out = build_expression_instruction(role='role',personality='personality',level_profile='',locale='en',interests=[],items=ITEMS,quiz_group=3)
    assert '원문을 맞히면 현지인 표현도' in out
    assert '각 문장 최대 2회' in out
    assert '최대 3번' not in out
    assert '처음부터 정답을 들려주고 따라 하게 하지 마라' not in out
    assert '되면 곧바로 다음 번호' not in out
    assert 'native 필드의 모국어 번역' in out


def test_chat_assembly_keeps_memory_without_always_questioning():
    args = dict(role='role',personality='personality',level_profile='',locale='en',interests=['등산'],close_tag='[통화종료]')
    out = build_chat_instruction(**args,memory={'topics':['등산'], 'facts':['산을 좋아함']})
    assert '모국어 질문에는 영어(English)로 답한 뒤 한국어 대화로 돌아온다.' in out
    assert '네 생각과 질문을 섞고' in out
    assert '산을 좋아함' in out
    assert '물음표로 끝내고 멈춰라' not in out
    assert '한국어로 인사한다' in seed_chat_opening('한국어', {'topics':['등산']})
    assert seed_chat_opening('한국어', {}) == ''


def test_native_copy_is_not_replaced_by_next_original():
    p=practice(); native_pending(p)
    p.observe_beaver('Next: "안녕하세요"')
    assert p.index == 0 and p.native_phrase == '정말 고마워요'
    p.observe_user('정말 고마워요')
    assert p.index == 1 and p.completed == {1}


def test_original_two_failures_do_not_create_native_or_mastery():
    p=practice(); p.observe_beaver('"감사합니다"')
    p.observe_user('모르겠어요'); assert p.original_attempts == 1
    p.observe_user('다시 모르겠어요')
    assert p.index == 1 and p.completed == {1} and p.native_phrase == ''


def test_native_two_failures_are_a_separate_limit():
    p=practice(); p.observe_beaver('"감사합니다"')
    p.observe_user('틀린 답'); p.observe_user('감사합니다')
    p.observe_beaver('"정말 고마워요"')
    p.observe_user('틀린 답'); assert p.index == 0 and p.native_attempts == 1
    p.observe_user('또 틀린 답'); assert p.index == 1


@pytest.mark.parametrize('text',['','  ','\n'])
def test_silence_preserves_native_phase_without_consuming_attempts(text):
    p=practice();native_pending(p);p.observe_user(text)
    assert p.phase == 'native' and p.native_attempts == 0


@pytest.mark.parametrize('utterance',['"Thank you"','"감사합니다"','"안녕하세요"','"정말 고마워요" and "아주 고마워요"'])
def test_translation_original_and_ambiguous_pair_are_not_registered(utterance):
    p=practice();p.observe_beaver('"감사합니다"');p.observe_user('감사합니다');p.observe_beaver(utterance)
    assert p.phase == 'native_intro' and p.native_phrase == ''


def test_fragment_replay_restores_exact_native_sentence_and_attempt_count():
    p=practice()
    p.replay([('beaver','"감사합니다"'),('user','감사합니다'),('beaver','"정말 고마워요"'),('user','틀린 답')])
    assert p.phase == 'native' and p.native_attempts == 1
    assert '정말 고마워요' in p.brief() and '1/2' in p.brief()
    p.observe_user('정말 고마워요');assert p.completed == {1}


@pytest.mark.parametrize('text',['자연스럽게는 정말 고마워요라고 해요. 한번 따라 말해 보세요.', 'Naturally, 정말 고마워요. Repeat it.'])
def test_native_phrase_does_not_require_quotation_marks(text):
    p=practice();p.observe_beaver('"감사합니다"');p.observe_user('감사합니다');p.observe_beaver(text)
    assert p.phase=='native' and p.native_phrase=='정말 고마워요'
    p.observe_user('정말 고마워요'); assert p.completed=={1}


def test_ambiguous_native_transcript_is_bounded_without_claiming_success():
    p=practice();p.observe_beaver('"감사합니다"');p.observe_user('감사합니다')
    p.observe_beaver('"정말 고마워요" or "아주 고마워요"')
    p.observe_user('틀린 답'); assert p.phase=='native_intro' and p.native_phrase==''
    p.observe_user('또 틀린 답'); assert p.index==1 and p.unresolved_native=={1}


def test_split_transcription_chunks_are_joined_by_actual_flush():
    s=state();native_pending(s.expr_practice)
    s.expr_practice=practice();s.expr_practice.observe_beaver('감사합니다');s.expr_practice.observe_user('감사합니다')
    s.cur_beaver_text=['자연스럽게는 정말 ', '고마워요라고 해요. 한번 따라 말해 보세요.']
    cs._flush_beaver_segment(s)
    assert s.expr_practice.native_phrase=='정말 고마워요'


def test_existing_resume_seeds_preserve_original_native_step():
    for seed in (seed_expression_resume(),brief_expression_silent_resume()):
        assert '미완료 원문 또는 현지인 복창 단계' in seed


def test_quiz_does_not_open_on_original_copy_or_assistant_next_intro(monkeypatch):
    s=state();monkeypatch.setattr(cs.svc,'EXPRESSION_QUIZ_GROUP',1)
    native_pending(s.expr_practice)
    s.covered_nums=[1,2]
    cs._expression_quiz_maybe_arm(s)
    assert s.expr_quiz_cue_pending is None
    s.expr_quiz_set=[1]
    assert cs._expression_quiz_cue_settled(s)[0] is False
    s.expr_practice.observe_user('정말 고마워요')
    cs._expression_quiz_maybe_arm(s)
    assert s.expr_quiz_set == [1] and s.expr_quiz_cue_pending
    assert cs._expression_quiz_cue_settled(s)[0] is True


def test_real_flush_tracks_copy_but_never_grants_quiz_pass():
    s=state();s.cur_beaver_text=['"감사합니다"'];cs._flush_beaver_segment(s)
    s.cur_user_text=['감사합니다'];cs._flush_user_segment(s)
    s.cur_beaver_text=['"정말 고마워요"'];cs._flush_beaver_segment(s)
    s.cur_user_text=['정말 고마워요'];cs._flush_user_segment(s)
    assert s.expr_practice.completed == {1}
    assert s.expr_quiz_pass == set() and s.expr_quiz_fail == set()
    assert s.covered_nums == [1]
    rows=cs._expression_result_snapshot(s,{10})
    assert len(rows)==1 and rows[0]['item_id']==10 and rows[0]['passed'] is False


def test_reconnect_keeps_native_state_and_brief():
    s=state();native_pending(s.expr_practice)
    cs._reset_turn_state_for_reconnect(s)
    assert s.expr_practice.phase=='native'
    assert '정말 고마워요' in cs._reconnect_brief(s)


def test_reground_and_drill_nudge_do_not_skip_pending_native():
    s=state();native_pending(s.expr_practice)
    assert '정말 고마워요' in cs._build_expression_note(s)
    session=AsyncMock()
    asyncio.run(cs._inject_drill_move_on(session,s))
    note=session.send_reground.call_args.args[0]
    assert '정말 고마워요' in note
    assert '그 표현은 충분히 했다' not in note


def test_normal_and_homework_do_not_get_an_expression_tracker():
    s=cs._CallState()
    assert s.expr_practice is None
    cs._note_drill_user_turn(s,'발화')
    assert s.expr_practice is None and s.expr_quiz_pass == set()


@pytest.mark.parametrize('item,copy',[
    ({'item_id':22,'obj':'사과','ex':'사과를 먹어요.','role':'vocab'},'사과를 먹어요.'),
    ({'item_id':22,'obj':'사과','ex':'사과를 먹어요.','role':'vocab'},'사과'),
    ({'item_id':23,'obj':'V-고 싶다','ex':'물을 마시고 싶어요.','role':'grammar'},'물을 마시고 싶어요.'),
])
def test_vocab_example_word_and_grammar_sentence_lead_to_native_step(item,copy):
    p=ExpressionPractice([item]);p.observe_beaver('Repeat: '+item['ex']);p.observe_user(copy)
    assert p.phase=='native_intro' and p.completed==set()
    assert p.original_attempts==1


def test_a_grammar_label_itself_is_not_a_practice_sentence():
    item={'item_id':23,'obj':'V-고 싶다','ex':'물을 마시고 싶어요.','role':'grammar'}
    p=ExpressionPractice([item]);p.observe_beaver(item['ex']);p.observe_user(item['obj'])
    assert p.phase=='original' and p.completed==set()


def test_explicit_native_phrase_may_also_be_a_future_original():
    p=ExpressionPractice([{'obj':'감사합니다'},{'obj':'고마워요'}])
    p.observe_beaver('감사합니다');p.observe_user('감사합니다')
    p.observe_beaver('Naturally: "고마워요". Repeat it.')
    assert p.phase=='native' and p.native_phrase=='고마워요'
    p.observe_user('고마워요')
    assert p.completed=={1} and p.index==1


def test_a_next_item_intro_does_not_register_a_native_pair():
    p=ExpressionPractice([{'obj':'감사합니다'},{'obj':'고마워요'}])
    p.observe_beaver('감사합니다');p.observe_user('감사합니다')
    p.observe_beaver('Next: "고마워요"')
    assert p.phase=='native_intro' and p.native_phrase==''
