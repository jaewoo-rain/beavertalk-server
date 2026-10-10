"""승인된 숙제 복습 대본. B2B 자료를 보존하며 개인 진도를 선별하지 않는다."""
from __future__ import annotations

import json
import copy

from core.prompts.common import (
    PERSONA_TAIL, RULE_CLOSE_PROTOCOL, RULE_RESPONSE_LENGTH,
    RULE_NONVERBAL_SOUND, RULE_OFF_TOPIC, locale_label,
)
from core.prompts.editable_loader import section


def normalize_homework(payload: dict) -> dict:
    """선정 완료 자료의 경계만 검증한다. 목표 순서와 원문은 바꾸지 않는다."""
    if not isinstance(payload, dict):
        raise ValueError("숙제 입력은 객체여야 함")
    source = payload.get("source")
    if source not in ("curriculum", "manual") or payload.get("language") != "ko":
        raise ValueError("숙제 출처와 한국어 자료가 필요함")
    assignment_id = payload.get("assignment_id")
    if type(assignment_id) is not int or assignment_id <= 0:
        raise ValueError("유효한 assignment_id가 필요함")
    activities = payload.get("activities")
    if (not isinstance(activities, list)
            or any(not isinstance(s, str) for s in activities)
            or "conversation" not in activities):
        raise ValueError("회화 활동이 활성화된 숙제여야 함")
    grammar = payload.get("grammar")
    if not isinstance(grammar, list) or any(
        not isinstance(s, str) or not s.strip() for s in grammar
    ):
        raise ValueError("실제 문법 목록이 필요함")
    goals = payload.get("goals")
    if not isinstance(goals, list) or not 1 <= len(goals) <= 10:
        raise ValueError("선정된 회화 목표 1~10개가 필요함")
    reference = "learning_item" if source == "curriculum" else "assignment_item"
    normalized = []
    seen = set()
    for goal in goals:
        if not isinstance(goal, dict) or goal.get("reference") != reference:
            raise ValueError("목표 출처가 숙제와 일치해야 함")
        item_id = goal.get("id")
        if type(item_id) is not int or item_id <= 0 or item_id in seen:
            raise ValueError("목표 ID가 유효하고 중복되지 않아야 함")
        seen.add(item_id)
        if not isinstance(goal.get("surface"), str) or not goal["surface"].strip():
            raise ValueError("목표 원문이 필요함")
        if source == "manual" and goal.get("conversation") is not True:
            raise ValueError("직접 출제 회화 선택이 필요함")
        item = {"reference": reference, "id": item_id, "surface": goal["surface"]}
        for key in ("meaning", "example", "example_meaning", "explanation"):
            if key in goal:
                value = goal[key]
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"{key}는 문자열 또는 null이어야 함")
                item[key] = value
        if source == "manual":
            item["conversation"] = True
        if "kind" in goal:
            if goal["kind"] is not None and goal["kind"] not in {"vocab", "grammar", "chunk"}:
                raise ValueError("실제 kind 분류가 유효해야 함")
            item["kind"] = goal["kind"]
        normalized.append(item)
    result = {
        "assignment_id": assignment_id, "source": source, "language": "ko",
        "activities": list(activities), "grammar": list(grammar), "goals": normalized,
    }
    if "attendance_targets" in payload:
        targets = payload["attendance_targets"]
        if not isinstance(targets, list):
            raise ValueError("전체 수행 대상 목록이 필요함")
        target_ids = set()
        by_id = {}
        for target in targets:
            if (not isinstance(target, dict) or target.get("reference") != reference
                    or type(target.get("id")) is not int or target["id"] <= 0
                    or target["id"] in target_ids
                    or not isinstance(target.get("surface"), str) or not target["surface"].strip()
                    or (source == "curriculum" and target.get("kind") not in {"vocab", "grammar", "chunk"})):
                raise ValueError("전체 수행 대상 출처와 분류가 유효해야 함")
            target_ids.add(target["id"])
            by_id[target["id"]] = target
        if source == "curriculum":
            for goal in normalized:
                target = by_id.get(goal["id"])
                if target is None or any(target.get(k) != goal.get(k) for k in ("surface", "kind")):
                    raise ValueError("점수 목표와 전체 수행 대상이 일치해야 함")
        result["attendance_targets"] = copy.deepcopy(targets)
    return result


def build_homework_instruction(
    *, role: str, personality: str, locale: str, name: str | None,
    homework: dict, face_rule: str = "",
) -> str:
    data = normalize_homework(homework)
    data.pop("attendance_targets", None)
    label = locale_label(locale)
    text = section("homework", "instruction").format(
        role=role or "", personality=personality or "", target="한국어",
        locale_label=label, homework_block=json.dumps(data, ensure_ascii=False, indent=2),
    )
    rules = "\n".join((
        PERSONA_TAIL.format(username=name or "학습자"),
        "2. " + RULE_CLOSE_PROTOCOL,
        RULE_RESPONSE_LENGTH.format(max_sentences=2), RULE_NONVERBAL_SOUND,
        RULE_OFF_TOPIC.format(target="한국어", locale_label=label),
    ))
    return text + "\n\n[불변 규칙]\n" + rules + ("\n\n" + face_rule if face_rule else "")


def seed_homework_opening() -> str:
    return section("homework", "seed_opening").format(target="한국어")


def build_homework_reground(homework: dict, locale: str) -> str:
    """재접지에서도 같은 원문을 제공한다. 사용 여부나 오답을 추정하지 않는다."""
    data = normalize_homework(homework)
    data.pop("attendance_targets", None)
    return (
        "[안내] 주어진 숙제 복습 대화를 이어가라. 답변에 반응하고 질문은 하나씩 하라. "
        f"막히거나 모국어로 물으면 {locale_label(locale)}로 짧게 돕고 한국어 대화로 돌아가라. "
        f"어떤 언어로 요청하든 번역·모국어 도움을 요청하면 목표 표현이나 현재 질문을 {locale_label(locale)}로 짧게 풀이한 뒤 한국어 대화로 돌아가라. "
        "자기소개나 제어 표기는 학습 도움을 대신하지 않으며 내부 안내·제어 표기는 답변에 노출하지 마라. "
        "grammar 표제·패턴·N/V 기호는 내부 참고로만 사용하고 읽거나 언급하지 마라. 모국어 도움도 표기 대신 목표 표현이나 현재 질문의 실제 문장을 짧게 풀이하라. "
        "숙제 목록이나 이 안내를 읽지 말고, 사용 여부와 정답을 추정하지 마라.\n"
        + json.dumps(data, ensure_ascii=False)
    )
