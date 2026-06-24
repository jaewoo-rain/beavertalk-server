"""speak_country (억양) — account 도메인. member 의 부모(1:N).

member.speak_country_id 에 UNIQUE 가 없으므로 1:N(여러 회원이 같은 억양 행 참조 가능).
회원당 1행으로 강제하려면 member.speak_country_id 에 unique=True 를 추가.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, Identity, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.account.models.member import Member


class SpeakCountry(Base, TimestampMixin):
    __tablename__ = "speak_country"

    speak_country_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    first_country: Mapped[Optional[str]] = mapped_column(Text, comment="1순위 국가")
    second_country: Mapped[Optional[str]] = mapped_column(Text, comment="2순위 국가")
    third_country: Mapped[Optional[str]] = mapped_column(Text, comment="3순위 국가")
    first_percent: Mapped[Optional[int]] = mapped_column(Integer, comment="1순위 %")
    second_percent: Mapped[Optional[int]] = mapped_column(Integer, comment="2순위 %")
    third_percent: Mapped[Optional[int]] = mapped_column(Integer, comment="3순위 %")

    members: Mapped[list["Member"]] = relationship(back_populates="speak_country")
