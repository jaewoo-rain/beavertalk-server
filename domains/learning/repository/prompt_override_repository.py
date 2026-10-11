# -*- coding: utf-8 -*-
"""프롬프트 override 조회 — 순수 SELECT(커밋하지 않는다, R3).

현역 = `name` 이 같고 `enabled=True` 인 **가장 최근 행**(append-only이므로 최신이 현역).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from domains.learning.models.prompt_override import PromptOverride


def active(db: Session, name: str) -> Optional[PromptOverride]:
    """그 대본의 현역 override. 없으면 `None`(= 코드 기본값을 쓴다).

    ⚠ 행이 하나도 없는 것이 **정상 상태**다 — 이 기능을 쓰지 않는 동안은 항상 None 이다.
    """
    key = (name or "").strip()
    if not key:
        return None
    return db.execute(
        select(PromptOverride)
        .where(PromptOverride.name == key, PromptOverride.enabled.is_(True))
        .order_by(PromptOverride.override_id.desc())
        .limit(1)
    ).scalars().first()
