"""⛔ 잠금 프롬프트 자산 해시(core/prompts/locked/*) — 누가 고치면 여기서 죽는다.

이 문장들은 서버 판정(quiz_judge)·표정(set_face)·진도(재접지·covered)·종료 규약 배관이 **그대로** 기대하는 것이다(gemini 2.5·3.1 모두).
터졌으면: 고치려던 게 «톤·설명» 이면 core/prompts/editable/*.md 로 가라(거기가 사람 자리다). 정말 잠금 문장을 바꿔야 하면 **bt-back 과 함께**
근거·시험을 같이 바꾸고 docs/prompts/README.md §8 에 적은 뒤 아래 해시를 갱신한다. 2026-09-12 잠금/편집 분리.
"""
from __future__ import annotations

import hashlib

import pytest

from core.prompts.locked import expression as lex
from core.prompts.locked import face, leveltest, normal, reground, rules, seeds
from core.prompts.locked import freetalk as lft

HELP = "⛔ 잠금 프롬프트가 바뀌었다 — 고치려면 bt-back 과 시험을 같이(톤·설명은 core/prompts/editable/*.md 에서). tests/test_prompt_locked_hash.py 독스트링"


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


FROZEN: dict[str, str] = {
    "rules.CONTROL_TAG": "704d9c1436e07cc1",
    "rules.CLOSE_TAG_DEFAULT": "cd355ad0f9e88723",
    "rules.PERSONA_TAIL": "c5c01df8675f7b03",
    "rules.RULE_CLOSE_PROTOCOL": "28b1488231414346",
    "rules.RULE_RESPONSE_LENGTH": "a1a581b11716485a",
    "rules.RULE_NONVERBAL_SOUND": "19e84ac833886ce0",
    "rules.RULE_OFF_TOPIC": "4ec3b6fb0c5d1eb8",
    "rules.RULE_OFF_TOPIC_BODY": "af6552c0c7e1c3a1",
    "face.FACE_TOOL_RULE": "d75b6a64e88efb57",   # 2026-09-12 qual(사장님 결정, bt-back 승인) — 옛 773ded08034bed22 는 FACE_TOOL_RULE_LEGACY
    "face.EMOTION_TAG_RULE": "2cef5ca860a68522",
    "face.FACE_TOOL_RULE_LEGACY": "773ded08034bed22",
    "face.SET_FACE_DESCRIPTION_LEGACY": "390b4609ee6359c4",
    "face.SET_FACE_EMOTION_DESCRIPTION_LEGACY": "75b9563f851249c7",
    "face.LANGUAGE_MARKER_RULE": "5a90e17616194924",
    "face.SET_FACE_DESCRIPTION": "e129ec356b1b6c18",   # 2026-09-12 qual — 옛 390b4609ee6359c4 는 *_LEGACY
    "face.SET_FACE_EMOTION_DESCRIPTION": "ea188ac798a6385a",   # 2026-09-12 qual(neutral 없음) — 옛 75b9563f851249c7 는 *_LEGACY
    "normal.STUDY_RESERVE_HEADER": "3d449452d7fe92d3",
    "normal.STUDY_FIVE_CHECK": "55533093e555338b",
    "normal.STUDY_FIVE_CHECK_L1_TAIL": "bc74fa51fa72c274",
    "normal.STUDY_NEXT_TAIL": "a42fc35c930ec159",
    "normal.KNOWN_GRAMMAR_FALLBACK": "8c87dcf09dedfacc",
    "normal.PROMOTION_NOTICE_TEMPLATE": "47bc5541db2c29da",
    "leveltest.LEVELTEST_PROCEDURE": "3a1016318df7a5c1",
    "leveltest.LEVELTEST_LADDER_KO": "8d87a4cf422dc6af",
    "leveltest.LEVELTEST_LADDER_JA": "f8a9755f57db995e",
    "leveltest.LEVELTEST_LADDER_EN": "4636d541d80547b7",
    "leveltest.LEVELTEST_LADDER_CN": "52fdae25bdc590cc",
    "leveltest.LEVELTEST_LADDER_FR": "4de537e0ea0aa06c",
    "leveltest.LEVELTEST_LADDER_VI": "2397e0a46bfe361c",
    "seeds.NUDGE_SEED_1_NORMAL": "a5a7aa91d07b6d56",
    "seeds.NUDGE_SEED_2_NORMAL": "53a8ea546b4fbec5",
    "seeds.NUDGE_SEED_1_LEVELTEST": "5028055967005e0f",
    "seeds.NUDGE_SEED_1_EXPRESSION": "3b1a66825d655134",
    "seeds.NUDGE_SEED_1_FREETALK": "9298ca123727e6ca",
    "seeds.NUDGE_SEED_1_FREETALK_LESSON": "c1099d50da05c663",
    "seeds.NUDGE_SEED_2_FREETALK": "38a077d8558c2831",
    "expression.EXPR_RULE3_ASK_FIRST": "f61cb45da0b3e608",
    "expression.EXPR_RULE3_LANDING": "7cf75ae6cbfacb3e",
    "expression.DRILL_GRAMMAR_LINE": "69f3569fd132c492",
    "expression.DRILL_REVEAL_LINE": "b478b53c075c3391",
    "expression.DRILL_FORMALITY_LINE": "26290a378fac6472",
    "expression.DRILL_SILENCE_LINE": "8433621e1a5d27ef",
    # 2026-09-13 실통화 1550 — bt-back 승인 2줄(다른 올바른 정중한 표현 인정 · 한 턴 요청 하나)
    "expression.DRILL_ALT_CORRECT_LINE": "5c0ae1f224b77db9",
    "expression.DRILL_ONE_ASK_LINE": "01cfaa899b1e48c1",
    "expression.QUIZ_LINE_1": "70e88636bce80ce3",
    "expression.QUIZ_LINE_2": "afbbc31838202091",
    "expression.ITEMS_HEADER": "f34fdcee428d7a32",
    "expression.ITEMS_LANGUAGE_NOTE": "093d2ebe6b76e246",
    "expression.ITEMS_EXHAUSTION_LINE": "72b10e569c8de7d9",
    "expression.CHARACTER_FRAME": "0e5e6aaa60166e64",
    # 2026-09-13 ja 배선 — 언어별 격식 줄(ko 는 DRILL_FORMALITY_LINE 그 객체)
    "expression.DRILL_FORMALITY_LINE_JA": "c96c24ac72584c11",
    # 2026-09-13 끊김 없는 조각 전환(사장님 결정 2) — silent 재개 브리프 마지막 줄. 종전 마지막 줄·build_resume_brief(silent=False) 는 바이트 불변(아래 FROZEN_FN)
    "reground.RESUME_SILENT_FIRST_ACTION": "c0e5dbe816b6c254",   # 2026-09-14 C5 «왔냐?»류 시작말 금지(silent 전용 줄 — 옛 f5b2bc59f9d9cb2e)
    # 2026-09-14 C1·C2 비ko 전용 드릴 줄(실통화 1601 ja) — ko procedure 는 아래 FROZEN_FN «expression.procedure» 5c0c6ac8e65fb352 그대로
    "expression.DRILL_TARGET_SCRIPT_LINE": "db851d31d1021909",
    "expression.DRILL_ASK_FIRST_LINE": "3e98c1a9d3f4a73a",
    # 2026-09-14 E(사장님 확정, 실통화 1592) — [문형] 항목은 같은 문형의 다른 올바른 문장도 정답. ko·ja 공통(has_grammar 일 때만)
    "expression.DRILL_GRAMMAR_ALT_LINE": "3b3c2f297047daad",
    "seeds.LOOP_BREAK_NOTE": "ca29880e45b4b46b",   # 2026-09-14 B 반복 루프 차단기(실통화 1602) — 새 문장, 기존 경로 무변경
}
MODULES = {"rules": rules, "face": face, "normal": normal, "leveltest": leveltest, "seeds": seeds, "expression": lex, "reground": reground}


@pytest.mark.parametrize("key", sorted(FROZEN))
def test_locked_constant_is_unchanged(key: str) -> None:
    mod, name = key.split(".", 1)
    if name == "DRILL_FORMALITY_LINE_JA":
        value = MODULES[mod].DRILL_FORMALITY_LINE_BY_LANGUAGE["ja"]
        assert MODULES[mod].DRILL_FORMALITY_LINE_BY_LANGUAGE["ko"] is MODULES[mod].DRILL_FORMALITY_LINE, "ko 격식 줄은 같은 객체(바이트 동일)"
    else:
        value = getattr(MODULES[mod], name)
    assert isinstance(value, str) and value
    assert _h(value) == FROZEN[key], f"{key}: {HELP}"


def test_locked_non_string_constants() -> None:
    assert _h("\n".join(lex.MODEL_BLOCK_31_LINES)) == "f7dd68410a28e537", f"[3.1 말투] {HELP}"
    # 2026-09-12(사장님 결정, bt-back 승인): neutral 제거 — 앱이 감정 클립 뒤 스스로 idle 복귀. 옛 6종은 SET_FACE_EMOTIONS_LEGACY.
    assert tuple(face.SET_FACE_EMOTIONS) == ("happy", "surprised", "sad", "angry", "laugh"), f"set_face enum — {HELP}"
    assert tuple(face.SET_FACE_EMOTIONS_LEGACY) == ("neutral", "happy", "surprised", "sad", "angry", "laugh"), f"set_face legacy enum — {HELP}"
    assert rules.DEFAULT_MAX_SENTENCES == 4 and rules.REGROUND_COVERED_CAP == 10, HELP


# --------------------------------------------------------------------------- #
# 함수형 잠금 — 고정 인자 출력 해시(재접지 쪽지·이어하기 시드·종료 시드·힌트 지시·퀴즈 큐·항목 렌더·차시 블록)
# --------------------------------------------------------------------------- #
class _Brief:
    situation = "처음 만난 반 친구와 이름과 나라 말하기"
    partner = "한국어 교실에서 처음 만난 반 친구"
    items = [{"obj": "인사말", "ex": "안녕히 계세요.", "role": "grammar"}, {"obj": "사람", "ex": "저 사람은 가수예요.", "role": "core"}]
    probes = ["마이클 씨는 어느 나라 사람이에요?"]


FROZEN_FN: dict[str, tuple[str, object]] = {
    "seeds.seed_resume": ("0843114577a4be22", lambda: seeds.seed_resume("한국어")),
    # 2026-09-14 C6 재개 쪽지 재작성(사장님 지시 형식 — 옛 3c789d831b2d6b81). 조각1 대본(seed_expression_opening·지시문)은 무변경.
    "seeds.seed_freetalk_lesson_reseed_short": ("7c5f1b1b661f473d", lambda: seeds.seed_freetalk_lesson_reseed_short("한국어")),   # 2026-09-15 P5(1610) 벙어리 인사 2번째 재시드
    "seeds.seed_expression_resume": ("8224b61c1b6e1983", lambda: seeds.seed_expression_resume("한국어")),
    "seeds.seed_expression_resume_mats": ("8a7218a5fb255229", lambda: seeds.seed_expression_resume("한국어", drilled=["물", "가다"], passed=["물"], failed=["가다"], recent=[("beaver", "물은 water 예요. 따라 해 볼까요?"), ("user", "물"), ("beaver", "좋아요! 다음은 가다.")])),
    "seeds.close_seed_normal": ("fbf515b5052b7c68", lambda: seeds.close_seed_normal("[통화종료:ab12]")),
    "seeds.close_seed_leveltest": ("082b0f3499072f05", lambda: seeds.close_seed_leveltest("[통화종료:ab12]")),
    # 2026-09-15 4차 C — 이미 다룬/남은 표현 목록판(서버가 항상 넘긴다 → ko 실통화 대본 바뀜). 목록 없는 호출은 아래 옛 해시 그대로.
    "seeds.expression_quiz_cue_lists": ("9f49a518eb8d1839", lambda: seeds.expression_quiz_cue("«물» «가다»", 2, retry=False, locale_label="영어(English)", target="한국어", done_labels=["물", "가다"], remaining_rows=["3. to go = 가다", "4. person = 사람"])),
    "reground.build_expression_reground_brief_remaining": ("6f100c295c08edfa", lambda: reground.build_expression_reground_brief("선생님", "다정", drilled=["물"], passed=["물"], failed=["가다"], next_label="사람", locale_label="영어(English)", remaining=["3. person = 사람"])),
    "seeds.expression_quiz_cue": ("beacb708da5b7955", lambda: seeds.expression_quiz_cue("«물» «가다»", 2, retry=False, locale_label="영어(English)", target="한국어")),
    "seeds.expression_quiz_set_reminder": ("8bcd024c38581f13", lambda: seeds.expression_quiz_set_reminder("«물» «가다»")),   # 2026-09-15 8차 C
    "seeds.expression_taught_judge_instruction_done": ("b0aa726021903a91", lambda: seeds.expression_taught_judge_instruction(["1. 물 — 뜻: water"], target="한국어", locale_label="영어(English)", done_rows=["2. 가다 — 뜻: to go"])),   # 2026-09-15 8차 C(done_rows 없으면 옛 해시)
    "seeds.expression_taught_judge_instruction": ("7025709c4196d049", lambda: seeds.expression_taught_judge_instruction(["1. 물 — 뜻: water"], target="한국어", locale_label="영어(English)")),   # 2026-09-15 4차 A
    "seeds.expression_quiz_verdict_instruction": ("dc5417c8d6bd1a25", lambda: seeds.expression_quiz_verdict_instruction(["1. 물 — 뜻: water"], target="한국어", locale_label="영어(English)")),   # 2026-09-15 4차 B
    "seeds.expression_quiz_fallback_instruction": ("ed351fe6f23314f9", lambda: seeds.expression_quiz_fallback_instruction(["1. 물 — 뜻: water"], target="한국어")),
    "reground.build_reground_reminder": ("33c3ac17e133a74b", lambda: reground.build_reground_reminder("선생님", "다정")),
    "reground.build_continue_reminder": ("383141a13f80c1b7", lambda: reground.build_continue_reminder("선생님", "다정")),
    "reground.build_reground_brief": ("3aead47678d36531", lambda: reground.build_reground_brief("선생님", "다정", mode="study", covered=["물"], topic="축구")),
    "reground.build_resume_brief": ("8c9bc152ae3ee5b9", lambda: reground.build_resume_brief(covered=["물"], strong=["가다"], weak=["-고 싶다"], topic="축구", pending="예문", facts=["학생"], summary="인사", curious="음식")),
    # 2026-09-13 끊김 없는 조각 전환 — silent 판(마지막 줄만 다름) · 표현학습 silent 쪽지(지시문 끝)
    "reground.build_resume_brief_silent": ("f724c2d58774fc46", lambda: reground.build_resume_brief(covered=["물"], strong=["가다"], weak=["-고 싶다"], topic="축구", pending="예문", facts=["학생"], summary="인사", curious="음식", silent=True)),
    "seeds.brief_expression_silent_resume": ("345afc0855c8d98d", lambda: seeds.brief_expression_silent_resume("한국어")),   # 2026-09-14 C5·C6(옛 757fed832c765c94)
    "seeds.brief_expression_silent_resume_mats": ("ff0f1576ecb30ae3", lambda: seeds.brief_expression_silent_resume("한국어", drilled=["물", "가다"], passed=["물"], failed=["가다"], recent=[("beaver", "물은 water 예요. 따라 해 볼까요?"), ("user", "물"), ("beaver", "좋아요! 다음은 가다.")])),
    "reground.build_expression_reground_brief": ("150e706f19a6f1ef", lambda: reground.build_expression_reground_brief("선생님", "다정", drilled=["물"], passed=["물"], failed=["가다"], next_label="사람", locale_label="영어(English)")),
    "reground.build_freetalk_reground_brief": ("ee645d3f88cccac6", lambda: reground.build_freetalk_reground_brief("상황", ["a", "b"], target="한국어")),
    "reground.reground_instruction": ("74b1fd05118e1639", lambda: reground.reground_instruction(["물", "가다"], "한국어")),
    "reground.hint_instruction_base": ("084abe0bba49d1d6", lambda: reground.hint_instruction_base("영어(English)", "한국어")),
    "reground.hint_lesson_clause": ("8ddecba239d5535d", lambda: reground.hint_lesson_clause(_Brief, "한국어")),
    "expression.model_block": ("689e4117233531ed", lambda: lex.model_block("3.1", target="한국어", locale_label="영어(English)")),
    "expression.render_item": ("fa3c4965acede421", lambda: lex.render_item(2, {"obj": "N입니까?, N입니다", "des": "formal", "ex": "저는 회사원입니다.", "role": "grammar"}) + "|" + lex.render_item(1, {"obj": "가다", "des": "to go", "ex": "학교에 가요"})),
    # 2026-09-14 E — has_grammar 판에 DRILL_GRAMMAR_ALT_LINE 1줄(옛 5c0c6ac8e65fb352). 문법 없는 차시(has_grammar=False)는 아래 procedure_nogrammar 그대로.
    "expression.procedure": ("547ec563b4c883bc", lambda: lex.procedure(drill_intro="- 드릴: {target}/{locale_label}", target="한국어", locale_label="영어(English)", has_grammar=True)),
    "expression.procedure_ja": ("158aaf1b72d5ac8a", lambda: lex.procedure(drill_intro="- 드릴: {target}/{locale_label}", target="일본어", locale_label="한국어", has_grammar=True, language="ja")),   # 2026-09-14 C1·C2(옛 a8eb11d5521147f2) + E(옛 f0fca9b954bd5ab4)
    "expression.procedure_nogrammar": ("ef2fd77e4fa85c7f", lambda: lex.procedure(drill_intro="- 드릴: {target}/{locale_label}", target="한국어", locale_label="영어(English)", has_grammar=False)),   # ko 무문법 차시 — 2026-09-14 기준(C·E 무영향)
    "freetalk.PROBE_NAME_RE_JA": ("4b9e8e9c58ee0730", lambda: lft.PROBE_NAME_RE_BY_LANGUAGE["ja"][0].pattern + "|" + lft.PROBE_NAME_RE_BY_LANGUAGE["ja"][1]),
    "reground.hint_reading_clause_ja": ("727769dcc6caac21", lambda: reground.hint_reading_clause("ja")),
    "expression.items_block": ("1704dbb54003a3ab", lambda: lex.items_block([{"obj": "물", "des": "water", "ex": None}], target="한국어", locale_label="영어(English)")),
    "normal.study_block": ("1b19e2de0fd2947c", lambda: normal.study_block([{"slot": "main", "kind": "grammar", "obj": "-고 싶다", "ex": "가고 싶어요", "des": "want", "state": "new"}, {"slot": "reserve", "kind": "chunk", "obj": "안녕히 가세요", "ex": None, "des": None, "state": "review", "this_call": True}], target="한국어", locale_label="영어(English)", lang_band="beginner")),
    "normal.study_block_l1": ("048df830e20011ef", lambda: normal.study_block([{"slot": "main", "kind": "chunk", "obj": "안녕하세요", "ex": None, "des": None, "state": "new"}], target="한국어", locale_label="영어(English)", lang_band="survival")),
    "normal.known_block": ("80d655861dfa293e", lambda: normal.known_block({"grammar": ["-아요"], "targets": [{"obj": "-고 싶다", "ex": "가고 싶어요", "hint": "주말"}]}, target="한국어", locale_label="영어(English)")),
    "normal.history_block": ("462a14ee6667b2c4", lambda: normal.history_block({"summaries": ["축구"], "expressions": ["안녕하세요"]})),
    "freetalk.lesson_block": ("d8957d859dac4820", lambda: lft.lesson_block(_Brief, username="John", header="[H]", partner_line="- 상대: {partner} — X", partner_fallback="F", material_line="- 소재: M", probes_prefix="- P:")),
}


@pytest.mark.parametrize("key", sorted(FROZEN_FN))
def test_locked_builder_output_is_unchanged(key: str) -> None:
    want, fn = FROZEN_FN[key]
    got = _h(fn())
    assert got == want, f"{key}: {HELP} (지금 {got})"
