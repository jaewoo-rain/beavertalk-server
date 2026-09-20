"""national_sound_stat (국적별 취약 소리 통계) — learning 도메인.

「같은 나라 화자들이 어려워하는 소리」 목록의 출처. 시드 전용이고 런타임 쓰기는 없다
(`scripts/seed_sound_lessons.py` 가 `assets/pronunciation/national_weak_sounds.json` 으로 upsert).

국가 식별은 ISO 2자리 + 영문 국가명을 **둘 다** 들고 있다. 국적 분류 API 는 `country`
(영문명)와 `iso` 를 주는데, `speak_country` 테이블에는 **영문명만** 저장돼 있다. 이름으로
찾을 수 있어야 해서 이름 컬럼에 인덱스를 둔다.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Identity, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class NationalSoundStat(Base, TimestampMixin):
    __tablename__ = "national_sound_stat"
    __table_args__ = (
        UniqueConstraint("country_iso", "sound_key", name="uq_national_sound_stat"),
    )

    national_sound_stat_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    country_iso: Mapped[str] = mapped_column(
        Text, nullable=False, index=True, comment="ISO 3166-1 alpha-2 (예: VN)",
    )
    country_name: Mapped[str] = mapped_column(
        Text, nullable=False, index=True,
        comment="영문 국가명 — speak_country.first_country 와 매칭하는 키",
    )
    sound_key: Mapped[str] = mapped_column(
        Text, nullable=False, comment="소리 키 — sound_lesson.sound_key 와 같은 값",
    )
    share: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="그 나라 화자 중 이 소리를 틀리는 비율 %",
    )
    rank: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="그 나라 안에서의 순위(1부터). 목록 정렬·추천 기준",
    )
