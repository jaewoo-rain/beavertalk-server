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
    if source == "manual" and "manual_kind" in payload:
        if payload["manual_kind"] not in (None, "word", "sentence"):
            raise ValueError("직접 출제 분류가 유효해야 함")
        result["manual_kind"] = payload["manual_kind"]
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
        elif result.get("manual_kind") in ("word", "sentence"):
            if any(target.get("conversation") is not True for target in targets):
                raise ValueError("직접 출제 전체 수행 대상의 회화 선택이 필요함")
            for goal in normalized:
                target = by_id.get(goal["id"])
                if target is None or any(target.get(key) != goal.get(key)
                                         for key in ("surface", "kind", "conversation")):
                    raise ValueError("직접 출제 목표와 전체 수행 대상이 일치해야 함")
        result["attendance_targets"] = copy.deepcopy(targets)
    roleplay = _normalize_roleplay(payload.get("roleplay"), source)
    if roleplay is not None:
        result["roleplay"] = roleplay
    return result


#: 역할극 네 칸 — 교사 콘솔 직접 만들기에서 Claude 가 만든 값(2026-10-13 PM-DEC-535).
#: b2b `conversation-materials` 최상위 `roleplay` 와 1:1(b2b developer 합의 10-13).
ROLEPLAY_FIELDS = ("topic", "situation", "ai_role", "learner_role")
_ROLEPLAY_MAX = 300


def _normalize_roleplay(value, source: str) -> dict | None:
    """역할극은 **덤**이다 — 모양이 어긋나면 버리고 일반 숙제 복습으로 진행한다.

    ⛔ 여기서 예외를 던지지 마라. 역할극 값 하나가 틀렸다고 숙제 통화 전체를 막으면
       학습자는 회화를 못 한다. 배울 표현(goals)이 이미 검증됐으면 통화는 성립한다.
    직접 출제(manual)만 받는다 — 커리큘럼 과제에는 교사가 쓴 역할극이 없다.
    """
    if source != "manual" or not isinstance(value, dict):
        return None
    out = {}
    for key in ROLEPLAY_FIELDS:
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            out[key] = text.strip()[:_ROLEPLAY_MAX]
    # 상대역이 없으면 역할극이 안 된다 — 상황·주제 없이 역할만 있어도 버린다.
    if "ai_role" not in out or not ("situation" in out or "topic" in out):
        return None
    return out


def _roleplay_block(roleplay: dict | None) -> str:
    if not roleplay:
        return ""
    lines = ["[역할극]"]
    if roleplay.get("topic"):
        lines.append(f"주제: {roleplay['topic']}")
    if roleplay.get("situation"):
        lines.append(f"상황: {roleplay['situation']}")
    lines.append(f"너의 역할: {roleplay['ai_role']}")
    if roleplay.get("learner_role"):
        lines.append(f"학습자의 역할: {roleplay['learner_role']}")
    lines.append(
        "이 상황 속 인물로 대화한다. 캐릭터의 말투·성격은 유지하되 너의 역할로 말하고, "
        "학습자를 학습자의 역할로 대한다. 상황을 끝까지 유지하며 [숙제 내용]의 회화 목표를 "
        "이 상황에서 자연스럽게 쓰게 이끈다. 역할극 설정을 읽거나 설명하지 않는다."
    )
    return "\n".join(lines)


def build_homework_instruction(
    *, role: str, personality: str, locale: str, name: str | None,
    homework: dict, face_rule: str = "",
) -> str:
    data = normalize_homework(homework)
    data.pop("attendance_targets", None)
    roleplay = data.pop("roleplay", None)
    label = locale_label(locale)
    text = section("homework", "instruction").format(
        role=role or "", personality=personality or "", target="한국어",
        locale_label=label, homework_block=json.dumps(data, ensure_ascii=False, indent=2),
    )
    # 역할극은 편집 템플릿 밖에서 붙인다 — 슬롯을 늘리면 편집본·기본본 슬롯 집합과
    # 토큰 예산(700) 검사가 함께 바뀐다. 없으면 한 글자도 안 붙어 기존 통화와 같다.
    block = _roleplay_block(roleplay)
    if block:
        text += "\n\n" + block
    rules = "\n".join((
        PERSONA_TAIL.format(username=name or "학습자"),
        "2. " + RULE_CLOSE_PROTOCOL,
        RULE_RESPONSE_LENGTH.format(max_sentences=2), RULE_NONVERBAL_SOUND,
        RULE_OFF_TOPIC.format(target="한국어", locale_label=label),
    ))
    return text + "\n\n[불변 규칙]\n" + rules + ("\n\n" + face_rule if face_rule else "")


def seed_homework_opening(homework: dict | None = None) -> str:
    text = section("homework", "seed_opening").format(target="한국어")
    roleplay = None
    if homework is not None:
        try:
            roleplay = normalize_homework(homework).get("roleplay")
        except ValueError:
            roleplay = None
    if roleplay:
        text += (f" 역할극이 있으면 인사와 첫 질문을 {roleplay['ai_role']}로서 [역할극] 상황에 맞게 한다. "
                 "역할극 설정은 읽지 않는다.")
    return text


def build_homework_reground(homework: dict, locale: str) -> str:
    """재접지에서도 같은 원문을 제공한다. 사용 여부나 오답을 추정하지 않는다."""
    data = normalize_homework(homework)
    data.pop("attendance_targets", None)
    roleplay = data.pop("roleplay", None)
    keep_role = (f"[역할극] 상황과 너의 역할({roleplay['ai_role']})을 계속 유지하라. "
                 if roleplay else "")
    return (
        "[안내] 주어진 숙제 복습 대화를 이어가라. 답변에 반응하고 질문은 하나씩 하라. "
        + keep_role +
        f"막히거나 모국어로 물으면 {locale_label(locale)}로 짧게 돕고 한국어 대화로 돌아가라. "
        f"어떤 언어로 요청하든 번역·모국어 도움을 요청하면 목표 표현이나 현재 질문을 {locale_label(locale)}로 짧게 풀이한 뒤 한국어 대화로 돌아가라. "
        "자기소개나 제어 표기는 학습 도움을 대신하지 않으며 내부 안내·제어 표기는 답변에 노출하지 마라. "
        "grammar 표제·패턴·N/V 기호는 내부 참고로만 사용하고 읽거나 언급하지 마라. 모국어 도움도 표기 대신 목표 표현이나 현재 질문의 실제 문장을 짧게 풀이하라. "
        "숙제 목록이나 이 안내를 읽지 말고, 사용 여부와 정답을 추정하지 마라.\n"
        + json.dumps(data, ensure_ascii=False)
    )
