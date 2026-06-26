"""voice (AI 음성) — commerce 도메인. 마스터 데이터.

캐릭터(페르소나)가 참조하는 실시간 통화 음성 카탈로그. Gemini Live 프리빌트
보이스(현재 30종)는 고정 목록이라 보이스명(name)을 유니크 식별자로 둔다.
캐릭터 ↔ 보이스 = N:1 (한 보이스를 여러 캐릭터가 쓸 수 있음).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, Identity, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.commerce.models.character import Character


class Voice(Base, TimestampMixin):
    __tablename__ = "voice"
    __table_args__ = (
        UniqueConstraint("name", name="uq_voice_name"),
    )

    voice_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, comment="Gemini Live 프리빌트 보이스명(예: Charon, Aoede)")
    description: Mapped[Optional[str]] = mapped_column(Text, comment="음색 설명(예: 밝은/차분한)")
    gender: Mapped[Optional[str]] = mapped_column(Text, comment="성별 느낌(male/female/neutral)")
    sample_url: Mapped[Optional[str]] = mapped_column(Text, comment="미리듣기 샘플 URL")

    characters: Mapped[list["Character"]] = relationship(
        back_populates="voice", lazy="select",
    )
