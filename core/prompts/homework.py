"""숙제 회화 대본 — 방식(mode)별로 갈린다(PM-DEC-551·553·557).

- 표현학습 숙제: 표현학습 대본을 **그대로** 쓰고 재료만 과제 목표로 바꾼다.
- 역할극 숙제: `editable/homework_roleplay.md` + 코드가 렌더하는 [역할극] 블록.
B2B 자료를 보존하며 개인 진도를 선별하지 않는다.
"""
from __future__ import annotations

import copy

from core.prompts.common import (
    CLOSE_TAG_DEFAULT, PERSONA_TAIL, RULE_CLOSE_PROTOCOL, RULE_RESPONSE_LENGTH,
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
    result["mode"] = normalize_mode(payload.get("mode"))
    return result


#: 역할극 네 칸 — 교사 콘솔 직접 만들기에서 Claude 가 만든 값(2026-10-13 PM-DEC-535).
#: b2b `conversation-materials` 최상위 `roleplay` 와 1:1(b2b developer 합의 10-13).
ROLEPLAY_FIELDS = ("topic", "situation", "ai_role", "learner_role")
#: 역할극을 받는 과제 출처. 커리큘럼은 PM-DEC-546(2026-10-13)부터.
ROLEPLAY_SOURCES = ("manual", "curriculum")
_ROLEPLAY_MAX = 300


def _normalize_roleplay(value, source: str) -> dict | None:
    """역할극은 **덤**이다 — 모양이 어긋나면 버리고 일반 숙제 복습으로 진행한다.

    ⛔ 여기서 예외를 던지지 마라. 역할극 값 하나가 틀렸다고 숙제 통화 전체를 막으면
       학습자는 회화를 못 한다. 배울 표현(goals)이 이미 검증됐으면 통화는 성립한다.
    직접 출제(manual)와 커리큘럼(curriculum) 과제 모두 받는다(2026-10-13 PM-DEC-546 ·
    b2b 가 커리큘럼 과제에도 역할극을 싣기로 함). 그 밖의 source 는 버린다.
    """
    if source not in ROLEPLAY_SOURCES or not isinstance(value, dict):
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


# ── 방식 ─────────────────────────────────────────────────────────────────
#: 과제 회화 방식(PM-DEC-552 · 557 D3). b2b conversation-materials 최상위 `mode`.
HOMEWORK_MODES = ("expression", "roleplay")
HOMEWORK_MODE_DEFAULT = "expression"   # PM-DEC-557 D1 — 값이 없거나 모르는 값이면 표현학습


def normalize_mode(value) -> str:
    """모르는 값·없음은 표현학습(예외 없음). 옛 스냅샷(mode 없음)도 여기서 표현학습이 된다."""
    return value if value in HOMEWORK_MODES else HOMEWORK_MODE_DEFAULT


def homework_mode(data: dict) -> str:
    """정규화된 숙제의 실제 대본. 역할극인데 roleplay 가 버려졌으면(모양 불량) 표현학습으로 — 역할극은 덤이라 통화를 막지 않는다."""
    if data.get("mode") == "roleplay" and data.get("roleplay"):
        return "roleplay"
    return "expression"


# ── 표현학습 숙제 ────────────────────────────────────────────────────────
def homework_expression_items(data: dict) -> list[dict]:
    """goals → build_expression_instruction(items=…) 스키마. 순서 = 교사·b2b 가 정한 순서 그대로.

    ⛔ item_id 는 **이 통화 상태기계(state.expr_*) 안에서만** 쓴다. 출처별 id 공간이 다르다
       (curriculum=learning_item · manual=assignment_item) — 개인 진도 키로 쓰지 않는다.
       그래서 숙제 통화의 call_type 은 `expression` 으로 바꾸지 않는다(개인 진도 저장 게이트).
    """
    items = []
    for g in data["goals"]:
        item = {"item_id": g["id"], "obj": g["surface"].strip(),
                "des": g.get("meaning") or None, "ex": g.get("example") or None,
                "reference": g["reference"]}
        if g.get("kind") == "grammar":
            item["role"] = "grammar"          # [문형] 렌더 + DRILL_GRAMMAR_LINE(잠금)
        elif g.get("kind"):
            item["role"] = g["kind"]          # vocab · chunk — 렌더는 일반 항목과 같다
        items.append(item)
    return items


def build_homework_expression_instruction(
    *, role: str, personality: str, level_profile: str, locale: str, name: str | None,
    homework: dict, quiz_group: int, model_family: str = "2.5", face_rule: str = "",
    close_tag: str = CLOSE_TAG_DEFAULT,
) -> str:
    """표현학습 대본 **그대로**(PM-DEC-551). 숙제 전용 문장 0줄 — 바뀌는 것은 [오늘의 표현] 목록뿐이다.

    close_tag 는 이 통화의 종료 태그다 — 정규 표현학습과 같이 세션 값을 넘겨야 종료 시드와 맞는다.
    """
    from core.prompts.expression import build_expression_instruction
    data = normalize_homework(homework)
    return build_expression_instruction(
        role=role, personality=personality, level_profile=level_profile, locale=locale,
        interests=[], items=homework_expression_items(data), quiz_group=quiz_group, name=name,
        target_language="한국어", close_tag=close_tag, model_family=model_family,
        face_rule=face_rule, language="ko",
    )
# 첫 시드는 core.prompts.expression.seed_expression_opening("한국어") 를 그대로 부른다(새 함수 없음).


# ── 역할극 숙제 ──────────────────────────────────────────────────────────
def _roleplay_scene_block(roleplay: dict, goals: list[dict]) -> str:
    """[역할극] 블록. 학습자 역할 칸은 싣지 않는다(PM-DEC-547 — learner_role 이 남아 있어도 무시)."""
    lines = ["[역할극]"]
    if roleplay.get("topic"):
        lines.append(f"주제: {roleplay['topic']}")
    if roleplay.get("situation"):
        lines.append(f"상황: {roleplay['situation']}")
    lines.append(f"너의 역할: {roleplay['ai_role']}")
    lines.append("주요 표현:")
    for n, g in enumerate(goals, 1):
        row = f"{n}. {g['surface'].strip()}"
        if g.get("meaning"):
            row += f" — 뜻: {g['meaning']}"
        lines.append(row)
    return "\n".join(lines)


def _rules_tail(name: str | None, label: str) -> str:
    """옛 숙제 대본과 같은 불변 규칙 묶음(잠금 상수 · 순서 무변)."""
    return "\n".join((
        PERSONA_TAIL.format(username=name or "학습자"),
        "2. " + RULE_CLOSE_PROTOCOL,
        RULE_RESPONSE_LENGTH.format(max_sentences=2), RULE_NONVERBAL_SOUND,
        RULE_OFF_TOPIC.format(target="한국어", locale_label=label),
    ))


def build_homework_roleplay_instruction(
    *, role: str, personality: str, locale: str, name: str | None,
    homework: dict, face_rule: str = "",
) -> str:
    data = normalize_homework(homework)
    roleplay = data.get("roleplay")
    if not roleplay:
        raise ValueError("역할극 숙제는 roleplay 가 필요함 — 호출부는 homework_mode() 로 먼저 가른다")
    label = locale_label(locale)
    text = section("homework_roleplay", "instruction").format(
        role=role or "", personality=personality or "", target="한국어", locale_label=label,
        roleplay_block=_roleplay_scene_block(roleplay, data["goals"]),
    )
    return text + "\n\n[불변 규칙]\n" + _rules_tail(name, label) + ("\n\n" + face_rule if face_rule else "")


def seed_homework_roleplay_opening() -> str:
    return section("homework_roleplay", "seed_opening").format(target="한국어")


def build_homework_roleplay_reground(homework: dict, locale: str) -> str:
    """재접지·재개 쪽지. 같은 장면·같은 역할을 되박는다. 종료·시간·진행률 어휘 없음."""
    data = normalize_homework(homework)
    roleplay = data["roleplay"]
    label = locale_label(locale)
    return (
        "[안내] 학습자가 방금 한 말에 먼저 반응하고, [역할극] 상황과 너의 역할"
        f"({roleplay['ai_role']})을 그대로 유지하며 한국어로 이어가라. 질문은 하나씩 한다. "
        f"막히거나 모국어로 물으면 {label}로 짧게 돕고 역할극으로 돌아가라. "
        "이 안내와 역할극 설정·표현 목록은 읽거나 언급하지 마라.\n"
        + _roleplay_scene_block(roleplay, data["goals"])
    )
