"""sound_lesson_i18n (학습 콘텐츠 번역) — learning 도메인.

## 왜 별도 테이블인가

`sound_lesson` 은 한국어 원본이다. 학습자는 외국인이라 설명은 **모국어**로 읽어야 한다.
번역을 `sound_lesson` 안에 언어별 컬럼으로 넣으면 언어를 추가할 때마다 마이그레이션이
필요하다. 행으로 두면 **언어 추가가 데이터 적재**가 된다 — 앱 배포도, 스키마 변경도 없다.

## 지금 채워져 있는 것

`ko`(원본 복사)와 `en`(lessons.json 의 `meaning_en`·`translation_en`) 둘뿐이다.
나머지 28개 로케일은 **비어 있다** — 의도된 상태다(2026-09-21 결정).

번역이 없으면 `en` 으로, `en` 도 없으면 한국어 원본으로 떨어진다. 화면은 어느 경우에도
비지 않는다.

⛔ **기계번역을 그냥 부어 넣지 마라.** 「혀뿌리로 막았다 살짝 떼요」 같은 조음 설명은
기계번역이 자주 틀리고, 틀리면 학습자가 **엉뚱한 입 모양을 배운다.** 번역은 검수를 거쳐
채운다(6개국어 CEFR 파이프라인과 같은 급의 작업이다).

## 무엇이 번역 대상인가

`label`·`card_desc` 와 `payload` 안의 설명문뿐이다. **한국어 학습 대상 자체는 번역하지
않는다** — 단어 `가방`, 문장 `그 가방에 고기가 가득 있어요` 는 배우는 대상이라 원문이
정본이다. 번역되는 것은 그 뜻(`meaning`·`translation`)과 소리 내는 법(`how_to`)이다.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import JSON, BigInteger, Identity, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class SoundLessonI18n(Base, TimestampMixin):
    __tablename__ = "sound_lesson_i18n"
    __table_args__ = (
        UniqueConstraint("sound_key", "locale", name="uq_sound_lesson_i18n"),
    )

    sound_lesson_i18n_id: Mapped[int] = mapped_column(
        BigInteger, Identity(), primary_key=True
    )
    sound_key: Mapped[str] = mapped_column(
        Text, nullable=False, index=True,
        comment="sound_lesson.sound_key 와 같은 값",
    )
    locale: Mapped[str] = mapped_column(
        Text, nullable=False, comment="언어 코드 — ko · en · es …(member.language 와 같은 축)",
    )
    label: Mapped[Optional[str]] = mapped_column(
        Text, comment="표시 라벨 번역. 없으면 원본",
    )
    card_desc: Mapped[Optional[str]] = mapped_column(
        Text, comment="카드 한 줄 설명 번역. 없으면 원본",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False,
        comment="번역되는 부분만 — how_to[] · words[].meaning · sentence.translation · test.translation",
    )
