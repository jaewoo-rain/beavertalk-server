"""member_level_history (레벨 변경 이력) — learning 도메인.

**created_at = 레벨 진입 시각의 단일 소스.** member 에 level_entered_at 컬럼을
만들지 마라(이중 소스 금지) — "현재 레벨 진입 시각"은 항상 이 테이블 최신 행의
created_at 파생값이다. 유효통화 수·체류일(G3)·승급 잠금(G5)·브리지 믹스·승급 멘트
전부 이 행 기준으로 계산한다.

- reason CHECK: placement(레벨테스트 배정) / gate_promotion(게이트 자동 승급) /
  remeasure_up·remeasure_down(재측정) / manual(운영 수동) — 마스터성 필드라 CHECK 유지.
- trigger_call_id UNIQUE(uq_mlh_trigger_call) = evaluate_level_up 멱등 키:
  같은 통화 분석이 재실행돼도 승급 1회만. NULL 다중 허용은 Postgres UNIQUE 기본
  (manual/placement 등 통화 없는 변경 다수 공존 가능).
- gate_snapshot: 승급 판정 당시 게이트 집계(JSON 문자열 — 프로젝트 TEXT-JSON 컨벤션,
  sqlite 테스트 호환. 감사·튜닝용).
- created_at 단독(TimestampMixin 미사용) — 이력 행은 불변, updated_at 무의미.
- 강등 없음: to_level < from_level 은 remeasure_down/manual 만 가능(앱 계층 보장).

설계: docs/20260709_1231_level-system-master-plan.md §3.3 / §5.3,
      docs/20260709_1346_level-system-detailed-mechanics.md ⑦~⑨.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class MemberLevelHistory(Base):
    __tablename__ = "member_level_history"
    __table_args__ = (
        CheckConstraint(
            "reason IN ('placement', 'gate_promotion', 'remeasure_up', 'remeasure_down', 'manual')",
            name="ck_mlh_reason",
        ),
        UniqueConstraint("trigger_call_id", name="uq_mlh_trigger_call"),
        # 회원별 이력 최신순 조회(현재 레벨 진입 시각 파생 — desc 정렬은 쿼리에서)
        Index("ix_mlh_member_created", "member_id", "created_at"),
    )

    history_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE", name="fk_mlh_member"),
        comment="회원",
    )

    from_level: Mapped[Optional[int]] = mapped_column(
        Integer, comment="변경 전 레벨(NULL=최초 배정 — placement)",
    )
    to_level: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="변경 후 레벨(1~13)",
    )
    reason: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="변경 사유(placement/gate_promotion/remeasure_up/remeasure_down/manual)",
    )
    trigger_call_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("call.call_id", ondelete="SET NULL", name="fk_mlh_trigger_call"),
        comment="승급을 유발한 통화(UNIQUE 멱등 키 — NULL 다중 허용)",
    )
    gate_snapshot: Mapped[Optional[str]] = mapped_column(
        Text, comment="판정 당시 게이트 집계 스냅샷(JSON 문자열 — 감사·튜닝용)",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        comment="레벨 진입 시각(단일 소스 — member.level_entered_at 금지)",
    )
