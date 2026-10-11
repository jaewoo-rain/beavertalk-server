# -*- coding: utf-8 -*-
"""프롬프트 override — 배포 없이 대본을 고치는 자리(2026-10-10 사장님 지시).

## 왜
대본 한 줄을 고치려면 빌드+배포 5~8분이 든다. 이 테이블에 글자를 넣으면 **다음 통화부터**
바로 적용된다(통화 중간에는 안 바뀐다 — 시작에 스냅샷을 뜬다).

## ⛔ 코드 기본값을 대체하지 않는다
`core/openai/prompts/<name>.py` 의 글자가 **진짜 기본값**이고, 이 테이블은 **덮어쓰기**다.
  ① 검사를 못 통과하면 코드 기본값으로 떨어진다(+ERROR 로그)
  ② `PROMPT_OVERRIDE_ENABLED` 가 꺼져 있으면 이 테이블을 아예 안 읽는다
  ③ 시험은 코드 기본값을 계속 지킨다 — DB 글자는 pytest 가 못 본다

## append-only
행을 **덮어쓰지 않는다.** 고칠 때마다 새 행을 넣고 `enabled` 로 현역을 가린다.
git 이력을 잃는 대신 이 테이블이 이력을 든다 — 「그 통화가 어느 글자로 돌았나」를
`call.prompt_override_id` 로 되짚을 수 있어야 하기 때문이다(2026-10-10 에 1815 와 1817 을
Cloud Run 리비전으로 갈라야 했던 그 불편을 없앤다).

## 무엇이 들어가나 — **대본 글자만**
데이터 블록(`[오늘의 표현]`·`[이번 차시]`·`[관심사]`·`[기억]`)은 **코드가 계속 만든다.**
그래서 라벨·데이터 짝이 저절로 지켜진다 — 대본에서 라벨 이름을 바꾸면 검사가 잡는다.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import BigInteger, Boolean, Identity, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class PromptOverride(Base, TimestampMixin):
    """대본 한 벌. `name` 당 `enabled=True` 인 **가장 최근 행**이 현역이다."""

    __tablename__ = "prompt_override"
    __table_args__ = (
        # 현역 조회가 `name` + `enabled` + 최신순이다 — 그 모양으로 인덱스를 둔다.
        Index("ix_prompt_override_name_enabled", "name", "enabled"),
        {"comment": "프롬프트 override(append-only) — 배포 없이 대본을 고친다"},
    )

    override_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # ⛔ 코드의 대본 이름과 **같은 글자**여야 한다: expression·freetalk·chat·leveltest.
    #   모르는 이름이면 아무 통화도 그걸 안 집는다(조용히 무시 — 로그로만 보인다).
    name: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="대본 이름(expression/freetalk/chat/leveltest) — 코드 모듈명과 같아야 한다",
    )
    # 대본 글자. 슬롯은 코드 기본값과 **같은 집합**이어야 한다(검사 ①).
    body: Mapped[str] = mapped_column(
        Text, nullable=False, comment="대본 전문. 슬롯 표기는 코드 기본값과 같아야 한다",
    )
    # ⚠ 기본이 False 다 — 넣어 두고 켜는 순서가 되게 한다(잘못 넣은 글자가 바로 안 나간다).
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false",
        comment="현역 여부. name 당 enabled=True 인 최신 행이 쓰인다",
    )
    note: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="무엇을 왜 바꿨나(사람이 읽는 메모)",
    )
