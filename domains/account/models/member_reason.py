"""member_reason (회원별 학습 이유) — account 도메인.

온보딩에서 다중 선택하는 "언어 학습 이유"를 회원당 N개 저장하는 1:N 테이블.
이유 자체는 고정 코드 집합([ALLOWED_REASONS])이라 별도 마스터 테이블 없이 코드 문자열로
보관한다(아이콘·문구 등 표시는 프론트 책임).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, Identity, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.account.models.member import Member

# 온보딩 학습 이유 코드(프론트 mockReasons 와 일치). 검증용 화이트리스트.
ALLOWED_REASONS: frozenset[str] = frozenset(
    {"travel", "career", "exam", "daily", "friends", "brain"}
)

# 통화 프롬프트의 "흥미·소재" 로 넣을 때 쓰는 사람이 읽을 라벨(한국어).
REASON_LABELS: dict[str, str] = {
    "travel": "여행",
    "career": "커리어·일",
    "exam": "시험 준비",
    "daily": "일상 생활",
    "friends": "친구·사람들",
    "brain": "자기계발",
}


class MemberReason(Base, TimestampMixin):
    __tablename__ = "member_reason"
    __table_args__ = (
        # 한 회원이 같은 이유를 중복 저장하지 못하게.
        UniqueConstraint("member_id", "reason", name="uq_member_reason"),
    )

    member_reason_id: Mapped[int] = mapped_column(
        BigInteger, Identity(), primary_key=True
    )
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[str] = mapped_column(Text, comment="학습 이유 코드")

    member: Mapped["Member"] = relationship(back_populates="reasons")
