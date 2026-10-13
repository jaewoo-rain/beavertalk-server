"""직접 출제 숙제 역할극(2026-10-13 PM-DEC-535) — b2b `conversation-materials.roleplay` 를 통화 지시문에 싣는다."""
import copy

from core.homework_contract import create_namespace, namespace_materials
from core.prompts.homework import (
    build_homework_instruction, build_homework_reground, normalize_homework, seed_homework_opening,
)

ROLEPLAY = {
    "topic": "시장에서 장보기",
    "situation": "전통 시장 과일 가게에서 사과를 산다",
    "ai_role": "과일 가게 사장님",
    "learner_role": "사과를 사러 온 손님",
}


def manual(roleplay=ROLEPLAY, source="manual"):
    reference = "assignment_item" if source == "manual" else "learning_item"
    goal = dict(reference=reference, id=73, surface="이거 얼마예요?", kind=None if source == "manual" else "chunk")
    if source == "manual":
        goal["conversation"] = True
    payload = dict(assignment_id=8, source=source, language="ko", activities=["conversation"],
                   grammar=[], goals=[goal], attendance_targets=[goal])
    if source == "manual":
        payload["manual_kind"] = "sentence"
    if roleplay is not None:
        payload["roleplay"] = copy.deepcopy(roleplay)
    return payload


def build(payload):
    return build_homework_instruction(role="비버 선생님", personality="다정함", locale="en",
                                      name="학습자", homework=payload)


def test_roleplay_block_lands_in_instruction():
    text = build(manual())
    assert "[역할극]" in text
    for value in ROLEPLAY.values():
        assert value in text
    # 숙제 JSON 블록에는 중복으로 싣지 않는다.
    assert '"roleplay"' not in text
    # 배울 표현은 그대로 [숙제 내용]에 있다.
    assert "이거 얼마예요?" in text


def test_without_roleplay_instruction_is_byte_identical():
    """역할극이 없으면 지금 운영 숙제 통화와 한 글자도 다르지 않다."""
    plain = manual(roleplay=None)
    assert build(plain) == build(dict(plain, roleplay=None))
    assert "[역할극]" not in build(plain)
    assert seed_homework_opening(plain) == seed_homework_opening()


def test_curriculum_roleplay_lands_in_instruction_and_opening():
    """PM-DEC-546: 커리큘럼 과제의 역할극도 지시문·첫마디·재접지에 싣는다."""
    payload = manual(source="curriculum")
    normalized = normalize_homework(payload)
    assert normalized["roleplay"] == ROLEPLAY
    text = build(payload)
    assert "[역할극]" in text and "너의 역할: 과일 가게 사장님" in text
    assert seed_homework_opening(payload) != seed_homework_opening()
    assert "과일 가게 사장님" in build_homework_reground(payload, "en")


def test_curriculum_without_roleplay_is_byte_identical():
    plain = manual(roleplay=None, source="curriculum")
    assert build(plain) == build(dict(plain, roleplay=None))
    assert "[역할극]" not in build(plain)
    assert seed_homework_opening(plain) == seed_homework_opening()


def test_unknown_source_ignores_roleplay():
    payload = manual(source="curriculum")
    payload["source"] = "survey"
    try:
        normalized = normalize_homework(payload)
    except ValueError:
        return  # 알 수 없는 출처는 계약 단계에서 거절된다 — 역할극까지 갈 일이 없다.
    assert "roleplay" not in normalized


def test_malformed_roleplay_never_blocks_the_call():
    for bad in ("문자열", [], {"topic": "주제만"}, {"ai_role": "사장님"}, {"ai_role": "  ", "situation": "x"},
                {"ai_role": 3, "situation": "x"}):
        data = normalize_homework(manual(roleplay=bad))
        assert "roleplay" not in data
        assert "[역할극]" not in build(manual(roleplay=bad))


def test_long_values_are_clipped():
    long = dict(ROLEPLAY, situation="가" * 1000)
    assert len(normalize_homework(manual(roleplay=long))["roleplay"]["situation"]) == 300


def test_seed_and_reground_keep_the_role():
    payload = manual()
    assert "과일 가게 사장님" in seed_homework_opening(payload)
    reground = build_homework_reground(payload, "en")
    assert "과일 가게 사장님" in reground
    assert '"roleplay"' not in reground


def test_snapshot_keeps_roleplay_for_resume():
    ns = create_namespace(manual(), manual_v2_enabled=True)
    assert ns["version"] == 2
    assert ns["snapshot"]["roleplay"] == ROLEPLAY
    resumed = normalize_homework(namespace_materials(ns))
    assert resumed["roleplay"] == ROLEPLAY


def test_snapshot_without_roleplay_unchanged():
    ns = create_namespace(manual(roleplay=None), manual_v2_enabled=True)
    assert "roleplay" not in ns["snapshot"]
