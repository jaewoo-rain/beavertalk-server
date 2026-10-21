"""숙제 대본 방식 분리(2026-10-13 PM-DEC-551·553·557) — 표현학습 숙제 · 역할극 숙제.

- 표현학습 숙제: 표현학습 대본 그대로 · 재료만 과제 목표(goals → items).
- 역할극 숙제: editable `homework_roleplay.md` + [역할극] 블록 · 학습자 역할 칸 없음.
"""
import copy

import pytest

from core.homework_contract import create_namespace, namespace_materials, snapshot_hash
from core.prompts.editable_loader import BANNED_WORDS
from core.prompts.expression import build_expression_instruction
from core.prompts.homework import (
    HOMEWORK_NO_NATIVE_LINE, build_homework_expression_instruction, build_homework_roleplay_instruction,
    build_homework_roleplay_reground, homework_expression_items, homework_mode,
    normalize_homework, seed_homework_roleplay_opening,
)

ROLEPLAY = {
    "topic": "시장에서 장보기",
    "situation": "전통 시장 과일 가게에서 사과를 산다",
    "ai_role": "과일 가게 사장님",
    "learner_role": "사과를 사러 온 손님",
}


def payload(roleplay=ROLEPLAY, source="manual", mode=None):
    reference = "assignment_item" if source == "manual" else "learning_item"
    goal = dict(reference=reference, id=73, surface="이거 얼마예요?", meaning="How much is this?",
                kind=None if source == "manual" else "chunk")
    if source == "manual":
        goal["conversation"] = True
    data = dict(assignment_id=8, source=source, language="ko", activities=["conversation"],
                grammar=[], goals=[goal], attendance_targets=[goal])
    if source == "manual":
        data["manual_kind"] = "sentence"
    if roleplay is not None:
        data["roleplay"] = copy.deepcopy(roleplay)
    if mode is not None:
        data["mode"] = mode
    return data


def roleplay_text(data):
    return build_homework_roleplay_instruction(role="비버 선생님", personality="다정함", locale="en",
                                               name="학습자", homework=data)


def test_no_mode_is_expression_and_byte_identical_to_expression_script():
    data = payload(mode=None)
    assert homework_mode(normalize_homework(data)) == "expression"
    kwargs = dict(role="비버 선생님", personality="다정함", level_profile="Basic A", locale="en",
                  name="학습자", quiz_group=3, model_family="2.5", face_rule="", close_tag="[끝-7f]")
    homework = build_homework_expression_instruction(homework=data, **kwargs)
    expression = build_expression_instruction(
        interests=[], items=homework_expression_items(normalize_homework(data)),
        target_language="한국어", language="ko", **kwargs)
    # PM-DEC-626 P12-b — 숙제는 현지인 단계 없음 한 줄만 덧붙는다(그 앞은 바이트 동일).
    assert homework == expression + "\n\n" + HOMEWORK_NO_NATIVE_LINE


@pytest.mark.parametrize("source", ["manual", "curriculum"])
def test_roleplay_mode_renders_scene_role_and_numbered_goals(source):
    data = payload(source=source, mode="roleplay")
    assert homework_mode(normalize_homework(data)) == "roleplay"
    text = roleplay_text(data)
    assert "[역할극]" in text
    assert "너의 역할: 과일 가게 사장님" in text
    assert "상황: 전통 시장 과일 가게에서 사과를 산다" in text
    assert "1. 이거 얼마예요? — 뜻: How much is this?" in text


def test_learner_role_never_reaches_prompt_seed_or_reground():
    data = payload(mode="roleplay")
    for text in (roleplay_text(data), seed_homework_roleplay_opening(),
                 build_homework_roleplay_reground(data, "en")):
        assert ROLEPLAY["learner_role"] not in text
        assert "학습자의 역할" not in text


@pytest.mark.parametrize("roleplay", [
    {"situation": "가게", "topic": "장보기"},          # ai_role 없음
    {"ai_role": "사장님"},                              # 상황·주제 없음
    None,
])
def test_broken_or_missing_roleplay_falls_back_to_expression(roleplay):
    assert homework_mode(normalize_homework(payload(roleplay=roleplay, mode="roleplay"))) == "expression"


def test_unknown_mode_is_expression():
    assert normalize_homework(payload(mode="debate"))["mode"] == "expression"


def test_roleplay_texts_have_no_banned_words():
    data = payload(mode="roleplay")
    for text in (roleplay_text(data), seed_homework_roleplay_opening(),
                 build_homework_roleplay_reground(data, "en")):
        assert [w for w in BANNED_WORDS if w in text] == []


def test_snapshot_round_trip_keeps_mode_and_roleplay():
    namespace = create_namespace(payload(mode="roleplay"), manual_v2_enabled=True)
    assert namespace["snapshot"]["mode"] == "roleplay"
    restored = normalize_homework(namespace_materials(namespace))
    assert restored["mode"] == "roleplay"
    assert restored["roleplay"]["ai_role"] == ROLEPLAY["ai_role"]
    assert homework_mode(restored) == "roleplay"


def test_old_snapshot_without_mode_restores_as_expression():
    namespace = create_namespace(payload(mode=None), manual_v2_enabled=True)
    namespace["snapshot"].pop("mode")
    namespace["snapshot_hash"] = snapshot_hash(namespace)
    assert normalize_homework(namespace_materials(namespace))["mode"] == "expression"


def test_mode_is_part_of_snapshot_hash():
    a = create_namespace(payload(mode="roleplay"), manual_v2_enabled=True)
    b = create_namespace(payload(mode="expression"), manual_v2_enabled=True)
    assert a["snapshot_hash"] != b["snapshot_hash"]


def test_values_over_300_chars_are_cut():
    long = dict(ROLEPLAY, situation="가" * 400)
    data = normalize_homework(payload(roleplay=long, mode="roleplay"))
    assert len(data["roleplay"]["situation"]) == 300


def test_reground_has_no_goal_list_and_keeps_learner_first_line():
    """call 2308 — 재접지 쪽지가 «주요 표현:» 목록을 다시 붙이면 재접지 뒤 표현을 굵게 늘어놓는다."""
    text = build_homework_roleplay_reground(payload(mode="roleplay"), "en")
    assert "주요 표현:" not in text and "이거 얼마예요?" not in text
    assert "주요 표현은 학습자가 먼저 말할 몫이다" in text
    assert "더 쉬운 한국어로 한 번만 다시 묻고" in text
    assert "너의 역할: 과일 가게 사장님" in text
    # 본 지시문에는 목록이 그대로 있다(재접지만 뺀다)
    assert "1. 이거 얼마예요? — 뜻: How much is this?" in roleplay_text(payload(mode="roleplay"))


def test_no_name_is_not_called_learner():
    """call 2308 첫 말 «학습자 너도» — 이름이 없으면 이름 칸에 «학습자» 를 넣지 않는다."""
    text = build_homework_roleplay_instruction(role="비버 선생님", personality="다정함", locale="en",
                                               name=None, homework=payload(mode="roleplay"))
    assert "대화상대의 이름은 학습자" not in text
    assert "대화상대의 이름은 아직 모른다" in text


def test_goal_list_is_wrapped_as_learner_first_and_beaver_praise_is_not_identity():
    """call 2310 — 본 지시문 목록을 비버가 먼저 꺼냄 · «beaver가 멋있어서» 에 정체 문구."""
    text = roleplay_text(payload(mode="roleplay"))
    assert "주요 표현(학습자가 먼저 말할 몫이다" in text and "일부러 먼저 꺼내거나" in text
    assert "1. 이거 얼마예요? — 뜻: How much is this?" in text
    assert "규칙 7의 정체 답은 정체를 직접 물을 때만 한다" in text
