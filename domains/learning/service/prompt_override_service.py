# -*- coding: utf-8 -*-
"""프롬프트 override 편집 — 목록·검사·올리기·내리기(2026-10-10 사장님 지시).

통화가 **읽는** 쪽은 `call_session._load_prompt_override` 다(현역 1행 + `vet`). 이 파일은
사람이 **쓰는** 쪽이다 — dev 편집기(`/__devprompt`)와 스크립트(`scripts/dev_prompt_override.py`)
가 둘 다 여기를 부른다. ⭐ **기본값 표(`DEFAULTS`)를 두 군데 두지 않기 위해** 서비스로 모았다.

## ⛔ 이 파일이 고르는 것, 고르지 않는 것
- 고른다: 어느 행이 현역인가 · 글자를 받아들일까(`vet` 위임) · 언제 커밋하나(R3)
- 고르지 않는다: **글자가 좋은가.** 검사는 배관만 본다 — 뜻은 통화로만 안다.

## append-only
고칠 때마다 **새 행**을 넣고 이전 현역을 내린다. 지우지 않는다 — `call.prompt_override_id`
로 「그 통화가 어느 글자로 돌았나」를 되짚어야 하기 때문이다.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from core.openai.prompts import chat as _chat
from core.openai.prompts import expression as _expression
from core.openai.prompts import freetalk as _freetalk
from core.openai.prompts import leveltest as _leveltest
from core.openai.prompts.override import MAX_LEN_FACTOR, vet
from domains.learning.models.prompt_override import PromptOverride
from domains.learning.repository import prompt_override_repository as repo
from domains.learning.service import normalcall_service as _nsvc

#: 대본 이름 → 코드 기본값.
#: ⛔ 여기 **없는 이름은 DB 로 못 바꾼다.** 대본을 떼는 일은 세 단계다:
#:   ① 그 모듈에 `SCRIPT_TEMPLATE` 를 만들고(조립 결과가 **바이트 동일**해야 한다)
#:   ② 빌더가 `script_override=` 를 받게 하고 ③ 여기 등록한다.
#: ⚠ `freetalk`·`chat`·`leveltest` 는 아직 ①을 안 했다 — 코드 안에만 있다.
DEFAULTS: dict[str, str] = {
    "expression": _expression.SCRIPT_TEMPLATE,
    "freetalk": _freetalk.SCRIPT_TEMPLATE,
    "chat": _chat.SCRIPT_TEMPLATE,
    # ⛔⛔ 레벨테스트만 성질이 다르다 — 이 글자는 **Gemini 정본과 같게** 맞춰 둔 것이고
    #   override 는 OpenAI 경로에만 걸린다. DB 로 고치면 두 엔진의 레벨테스트가 갈라진다.
    #   레벨 판정을 두 엔진으로 비교하는 QA 에서는 그걸 알고 봐야 한다.
    "leveltest": _leveltest.SCRIPT_TEMPLATE,
}

#: 미리보기용 가짜 재료. ⭐ 목적은 **데이터 블록이 어디에 붙나**를 눈으로 보는 것이다 —
#:   대본이 `[오늘의 표현]` 을 이름으로 가리키므로, 라벨을 건드리면 여기서 짝이 깨져 보인다.
#: ⚠ **7개**다 — 3개 묶음이 두 번 차고 꼬리 1개가 남는 수. 묶음 줄(▶)과 꼬리 묶음이
#:   어떻게 보이는지 미리보기에서 확인하려면 묶음 수보다 **많아야** 한다(3개면 묶음이 하나뿐).
_PREVIEW_ITEMS = [
    {"obj": "고마워요", "des": "thank you", "ex": "도와줘서 고마워요."},
    {"obj": "괜찮아요", "des": "it's okay / no thanks", "ex": "저는 괜찮아요."},
    {"obj": "잠시만요", "des": "just a moment", "ex": "잠시만요, 금방 올게요."},
    {"obj": "저는 ◯◯이에요", "des": "I'm ◯◯", "ex": "저는 민수예요."},
    {"obj": "다시 말해 주세요", "des": "please say it again", "ex": "다시 말해 주세요."},
    {"obj": "얼마예요?", "des": "how much is it?", "ex": "이거 얼마예요?"},
    {"obj": "맛있어요", "des": "it's delicious", "ex": "이 김치 정말 맛있어요."},
]


def names() -> list[str]:
    """DB 로 바꿀 수 있는 대본 이름(= 떼어낸 대본)."""
    return sorted(DEFAULTS)


def default_of(name: str) -> str:
    """그 대본의 **코드 기본값**. 모르는 이름이면 `KeyError`."""
    return DEFAULTS[(name or "").strip()]


def limit_of(name: str) -> int:
    """길이 상한 — 검사와 **같은 식**을 쓴다(화면에 상한을 보여주기 위해)."""
    return max(len(default_of(name)) * MAX_LEN_FACTOR, 400)


def _row_dict(row: PromptOverride) -> dict[str, Any]:
    return {
        "override_id": row.override_id,
        "name": row.name,
        "enabled": bool(row.enabled),
        "length": len(row.body or ""),
        "note": row.note or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def state(db: Session) -> dict[str, Any]:
    """편집기 첫 화면 — 대본별 현역 여부·기본값 길이·이력 몇 줄."""
    out = []
    for name in names():
        active = repo.active(db, name)
        history = db.execute(
            select(PromptOverride)
            .where(PromptOverride.name == name)
            .order_by(PromptOverride.override_id.desc())
            .limit(20)
        ).scalars().all()
        out.append({
            "name": name,
            "default_length": len(default_of(name)),
            "limit": limit_of(name),
            "active": _row_dict(active) if active else None,
            "history": [_row_dict(r) for r in history],
        })
    return {"scripts": out}


def body_of(db: Session, name: str) -> dict[str, Any]:
    """편집 출발점 — 현역이 있으면 **그 글자**, 없으면 코드 기본값.

    ⚠ 「코드 기본값」을 같이 돌려준다. 편집기가 **되돌리기** 버튼을 그걸로 만든다.
    """
    default = default_of(name)
    active = repo.active(db, name)
    return {
        "name": name,
        "body": (active.body if active else default),
        "source": ("override" if active else "code"),
        "active": _row_dict(active) if active else None,
        "default": default,
        "limit": limit_of(name),
    }


def check(name: str, body: str) -> dict[str, Any]:
    """검사 + **조립 미리보기**. DB 를 건드리지 않는다(읽지도 쓰지도 않는다).

    ⭐ 미리보기가 이 화면의 핵심이다 — 슬롯이 실제 값으로 채워진 모습과, 코드가 붙이는
      데이터 블록(`[오늘의 표현]`)이 **어디에 붙나**를 같이 보여준다. 글자만 보고 올리면
      「대본은 그럴듯한데 데이터가 안 붙는」 사고를 못 잡는다.
    """
    default = default_of(name)
    ok, why = vet(name, body or "", default)
    preview = ""
    error = ""
    if ok:
        try:
            preview = _assemble(name, body)
        except Exception as exc:  # noqa: BLE001 — 포맷 오류를 화면에 그대로 보여준다
            # ⚠ 검사를 통과했는데도 터지면 **검사에 구멍이 있다는 뜻**이다. 올리기를 막고
            #   이유를 그대로 보여준다(조용히 통과시키면 통화가 열리다 죽는다).
            ok, error = False, "조립이 터졌다(검사에 없는 구멍): %s" % exc
    return {
        "ok": ok,
        "reason": why or error,
        "length": len(body or ""),
        "default_length": len(default),
        "limit": limit_of(name),
        "preview": preview,
    }


def _assemble(name: str, body: str) -> str:
    """가짜 재료로 그 대본을 끝까지 조립한다 — **통화가 받는 모양 그대로**.

    ⚠ 재료는 「있는 경우」로 넣는다 — 데이터 블록이 **붙은** 모습을 봐야 라벨 계약이
      깨졌는지 눈에 보인다. 빈 재료로 미리보기를 만들면 블록이 아예 빠져 아무것도 못 본다.
    """
    if name == "expression":
        return _expression.build_expression_instruction(
            script_override=body,
            role="비버 선생님", personality="장난스럽고 다정하다",
            locale_label="영어(English)", items=_PREVIEW_ITEMS,
            # ⭐ 「3개마다 복습」의 그 수 — 통화가 쓰는 그 상수를 그대로 쓴다
            #   (`call_session` 이 `svc.EXPRESSION_QUIZ_GROUP` 로 넘기는 것과 같은 값).
            quiz_group=_nsvc.EXPRESSION_QUIZ_GROUP,
            target_language="한국어", name="재우", face_rule="",
        )
    if name == "freetalk":
        return _freetalk.build_freetalk_instruction(
            script_override=body,
            role="비버 선생님", personality="장난스럽고 다정하다",
            locale_label="영어(English)", situation="카페에서 커피 주문하기",
            partner="카페 직원",
            items=[dict(it, role="chunk") for it in _PREVIEW_ITEMS],
            probes=["마이클 씨는 어떤 커피를 좋아해요?"],
            target_language="한국어", name="재우", face_rule="",
        )
    if name == "chat":
        return _chat.build_chat_instruction(
            script_override=body,
            role="비버 선생님", personality="장난스럽고 다정하다",
            locale_label="영어(English)", interests=["여행", "요리"],
            # ⭐ 기억을 **넣어서** 미리본다 — 대본의 「제공된 기억이 있으면」 갈래와
            #   `[기억]` 블록이 짝지어지는 모습이 이 화면의 확인거리다.
            memory={"summary": "지난주에 부산에 다녀왔다고 했다",
                    "facts": ["고양이를 키운다"], "next_topics": ["부산에서 뭘 먹었나"]},
            target_language="한국어", name="재우", face_rule="",
        )
    if name == "leveltest":
        return _leveltest.build_leveltest_instruction(
            script_override=body,
            role="비버 선생님", personality="장난스럽고 다정하다",
            locale="en", interests=["여행", "요리"], name="재우",
            target_language="한국어",
        )
    raise KeyError("미리보기를 모르는 대본: %s" % name)


def put(db: Session, name: str, body: str, note: str = "") -> dict[str, Any]:
    """검사를 통과하면 **새 행**을 넣고 현역으로 세운다. 거부되면 아무것도 쓰지 않는다.

    ⛔ 커밋은 여기서 한다(R3). repository 는 커밋하지 않는다.
    """
    result = check(name, body)
    if not result["ok"]:
        return {"ok": False, "reason": result["reason"]}

    # 이전 현역을 내린다 — 지우지 않는다(append-only). 조회는 최신 1행만 보므로 이 UPDATE 를
    # 빼도 동작은 같지만, **표를 눈으로 봤을 때 현역이 하나**여야 사람이 헷갈리지 않는다.
    db.execute(
        update(PromptOverride)
        .where(PromptOverride.name == name, PromptOverride.enabled.is_(True))
        .values(enabled=False)
    )
    row = PromptOverride(name=name, body=body, enabled=True, note=(note or "").strip() or None)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"ok": True, "row": _row_dict(row), "preview": result["preview"]}


def disable(db: Session, name: str) -> dict[str, Any]:
    """현역을 내려 **코드 기본값으로 되돌린다**. 행은 남는다."""
    n = db.execute(
        update(PromptOverride)
        .where(PromptOverride.name == name, PromptOverride.enabled.is_(True))
        .values(enabled=False)
    ).rowcount
    db.commit()
    return {"ok": True, "disabled": int(n or 0)}


def enable(db: Session, name: str, override_id: int) -> dict[str, Any]:
    """이력에 있는 **옛 행으로 되돌린다**(그 행을 현역으로 세운다).

    ⭐ 되돌리기가 이 테이블의 존재 이유 절반이다 — 통화가 이상해지면 직전 글자로 1초에
      돌아갈 수 있어야 한다. 그래서 행을 지우지 않는다.
    """
    row: Optional[PromptOverride] = db.get(PromptOverride, override_id)
    if row is None or row.name != name:
        return {"ok": False, "reason": "그 대본에 없는 행: id=%s" % override_id}
    # ⚠ 옛 글자도 **다시 검사한다** — 코드 기본값이 그 사이 바뀌어 슬롯이 어긋났을 수 있다.
    ok, why = vet(name, row.body or "", default_of(name))
    if not ok:
        return {"ok": False, "reason": "옛 글자가 지금 코드와 안 맞는다 — %s" % why}
    db.execute(
        update(PromptOverride)
        .where(PromptOverride.name == name, PromptOverride.enabled.is_(True))
        .values(enabled=False)
    )
    row.enabled = True
    db.commit()
    return {"ok": True, "row": _row_dict(row)}


def row_body(db: Session, name: str, override_id: int) -> Optional[str]:
    """이력 한 줄의 글자(편집기에서 옛 글자를 불러올 때)."""
    row: Optional[PromptOverride] = db.get(PromptOverride, override_id)
    return row.body if (row is not None and row.name == name) else None
