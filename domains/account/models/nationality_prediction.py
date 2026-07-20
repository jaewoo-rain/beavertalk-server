"""nationality_prediction (국적 예측 이력) — account 도메인.

매 통화의 user 음성으로 외부 국적 API 를 호출한 결과를 append 로 남긴다.
회원별 최근 N개(FIFO)를 평균해 SpeakCountry(억양)를 재계산하는 원본 이력.

- predictions: 외부 API 원응답의 예측 배열 [{country, iso, prob}] (JSON — JSONB 금지,
  sqlite 테스트 호환). NOT NULL(예측 없으면 애초에 행을 만들지 않는다).
- top1: 최상위 예측 **국가명**(표시·디버깅용 캐시 — predictions[0] 파생).
- call_id: 예측을 유발한 통화. 통화 삭제 시 이력은 보존(SET NULL). UNIQUE(멱등 —
  통화당 예측 1회).
- 단방향(back_populates 없음) — Member 는 무수정. 필요 시 쿼리에서 join.
- ix_ncp_member_created: 회원별 최근 N개(FIFO) 조회 커버.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    ForeignKey,
    Identity,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.account.models.member import Member
    from domains.learning.models.call import Call


class NationalityPrediction(Base, TimestampMixin):
    __tablename__ = "nationality_prediction"
    __table_args__ = (
        UniqueConstraint("call_id", name="uq_ncp_call"),
        Index("ix_ncp_member_created", "member_id", "created_at"),
    )

    prediction_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE", name="fk_ncp_member"),
        index=True, comment="회원",
    )
    call_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("call.call_id", ondelete="SET NULL", name="fk_ncp_call"),
        comment="예측을 유발한 통화(통화 삭제 시 이력 보존 — SET NULL)",
    )
    predictions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, comment="[{country,iso,prob}] — 외부 국적 API 원응답 예측 배열",
    )
    top1: Mapped[Optional[str]] = mapped_column(
        Text, comment="최상위 예측 국가명(predictions[0] 파생 캐시)",
    )

    # 단방향(back_populates 없음) — Member 는 무수정.
    member: Mapped["Member"] = relationship(lazy="select")
    call: Mapped[Optional["Call"]] = relationship(lazy="select")
