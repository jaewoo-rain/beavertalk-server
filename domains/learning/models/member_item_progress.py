"""member_item_progress (체크판 — 회원×학습항목 숙달 진행) — learning 도메인.

**희소(sparse) 테이블**: 행 부재 = UNSEEN(미학습). 행은 첫 증거/주입 때 생기고,
이후 introduced → practicing → mastered 로 전이한다(강등 없음 — fast-track 미확정
복귀가 유일한 예외). 상태·점수·카운터는 전부 item_evidence(append-only 원본)의
파생값 — 규칙이 바뀌면 증거 로그 리플레이로 전체 재계산 가능해야 한다.

- status: CHECK 없음 — call.status 컨벤션(TEXT + server_default, 화이트리스트는 앱 계층).
  unseen 은 행 부재로 표현하므로 값 목록에 없다.
- provenance: observed(실증거) / placement(레벨테스트 grandfathering) / fast_track(직행).
  placement 는 실증거가 오면 즉시 굴복(F 1건 → practicing score 1.0).
- fast_track_confirmed_at: NULL=미확정 — 미확정분은 레벨업 G2(숙달 게이트) 미산입.
- UNIQUE(member_id, item_id) = 회원당 항목 1행(uq_member_item).
- ix_mip_member_status_used: 통화 재료 선별(복습/유도 큐 — 상태별 최근 사용순) 커버.
- ⚠ relationship 은 단방향 최소(member/item, lazy select)만 둔다.
  **Member.progress 컬렉션을 만들지 마라** — 회원당 수천 행(어휘 1만+)이라
  컬렉션 로드는 곧 N+1/폭탄 쿼리. 집계는 항상 repository 쿼리로.

설계: docs/20260709_1231_level-system-master-plan.md §3.3 / §5,
      docs/20260709_1346_level-system-detailed-mechanics.md ⑤~⑦.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.account.models.member import Member
    from domains.learning.models.learning_item import LearningItem


class MemberItemProgress(Base, TimestampMixin):
    __tablename__ = "member_item_progress"
    __table_args__ = (
        UniqueConstraint("member_id", "item_id", name="uq_member_item"),
        # 통화 재료 선별(상태별 최근 사용순 — 복습·유도 큐)
        Index("ix_mip_member_status_used", "member_id", "status", "last_used_at"),
    )

    progress_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE", name="fk_mip_member"),
        comment="회원",
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("learning_item.item_id", ondelete="CASCADE", name="fk_mip_item"),
        index=True, comment="학습 항목",
    )

    # ── 상태 (CHECK 없음 — call.status 컨벤션, unseen 은 행 부재) ──
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'introduced'"),
        comment="숙달 상태(introduced/practicing/mastered — unseen 은 행 부재)",
    )
    score: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0"),
        comment="숙달 점수(0~6 앱 클램프 — E0+0.25/E1+0.5/E2+1.0/E3+1.5/F-1.0)",
    )
    provenance: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'observed'"),
        comment="상태 출처(observed/placement/fast_track)",
    )
    fast_track_confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        comment="fast-track 확정 시각(NULL=미확정 — 미확정분은 G2 게이트 미산입)",
    )

    # ── 증거 카운터 (item_evidence 파생 집계) ──
    repeat_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="모방(E1) 누적 횟수",
    )
    prompted_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="유도 성공(E2) 누적 횟수",
    )
    spontaneous_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="자발 사용(E3) 누적 횟수",
    )
    miss_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="오류(F) 누적 횟수",
    )

    # ── 시각 ──
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        comment="최초 노출/학습 시각",
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        comment="최근 노출 시각(주입 포함)",
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="학습자가 최근 실사용한 시각(E1+ 증거)",
    )
    mastered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="MASTERED 도달 시각",
    )

    # ── 통화 귀속 (통화 삭제 시 진행은 보존 — SET NULL) ──
    first_call_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("call.call_id", ondelete="SET NULL", name="fk_mip_first_call"),
        comment="최초 증거 통화",
    )
    last_call_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("call.call_id", ondelete="SET NULL", name="fk_mip_last_call"),
        comment="최근 증거 통화",
    )

    # ── 단방향 최소 relationship — Member.progress 역컬렉션 금지(N+1 방지) ──
    member: Mapped["Member"] = relationship(lazy="select")
    item: Mapped["LearningItem"] = relationship(lazy="select")
