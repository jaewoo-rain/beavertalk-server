"""call_raw_data (전화 원본 음성 데이터) — learning 도메인. call 과 1:N."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, ForeignKey, Identity, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.learning.models.call import Call


class CallRawData(Base, TimestampMixin):
    __tablename__ = "call_raw_data"
    # ⭐ 13차 B(2026-09-19, 사장님 «걸어») — 한 통화의 같은 턴은 한 행뿐이다. 12차 A 가 중복을 만든 경합을 코드에서 없앴고
    #   옛 중복 400행(통화 39건)은 13차 A 에서 지웠다 ⇒ 이제 DB 가 재발을 막는다(docs/20260919_0930_전사-중복-저장-정리.md).
    #   ⚠ turn_index 는 nullable 이고 Postgres 는 NULL 을 서로 다른 값으로 본다 — 번호 없는 옛 행은 제약에 걸리지 않는다.
    __table_args__ = (UniqueConstraint("call_id", "turn_index", name="uq_call_raw_data_call_turn"),)

    call_raw_data_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    call_id: Mapped[int] = mapped_column(
        ForeignKey("call.call_id", ondelete="CASCADE"), index=True, comment="통화",
    )
    role: Mapped[Optional[str]] = mapped_column(Text, comment="화자(user/beaver)")
    turn_index: Mapped[Optional[int]] = mapped_column(Integer, comment="턴 순서(0부터)")
    content: Mapped[Optional[str]] = mapped_column(Text, comment="음성 데이터 전사")
    voice_url: Mapped[Optional[str]] = mapped_column(Text, comment="보이스 데이터 저장 위치")
    total_time: Mapped[Optional[int]] = mapped_column(Integer, comment="음성 시간(초)")

    call: Mapped["Call"] = relationship(back_populates="raw_data")
