"""cur_text_i18n (커리큘럼 표시 문구 번역 캐시) — learning 도메인. §6(2026-09-29).

`cur_lesson.situation`(상황 한 줄)은 한국어 한 벌이다(ja 과정도 한국어). 앱 홈 학습 카드가
한국어 줄 아래에 회원 모국어 번역을 병기한다(`GET /cur/me` `lesson.situation_translation`).

## 왜 이 모양인가
- ⭐ **요청 시 번역 + 캐시** — 전량(836 문장 × 29언어) 미리 채우지 않는다. 회원이 실제로
  닿은 (문장, 언어)만 번역해 여기 쌓는다(`curriculum_i18n.situation_translation`).
- ⭐ 키는 **원문 문자열**(`source`)이다 — lesson_id·code 가 아니다. 재시드로 id·code·문구가
  바뀔 수 있는데, 번역은 «그 한국어 문장»의 함수라 원문 키가 가장 안전하다(문구가 바뀌면 자연히
  새 번역을 만든다). ko 과정과 ja 과정이 같은 문장을 쓰면 1행을 공유한다.
- `kind` 로 무엇의 번역인지 가른다 — 지금은 `cur_situation` 하나. `cur_topic.name` 같은 다른
  한국어 표시 문구가 필요해지면 kind 만 달리 넣는다(⛔ 요청 밖이라 지금은 안 넣는다).
- ⛔ `ko` 행 금지(CHECK) — 원문이 한국어다. 원본이 두 곳에 생기면 갈린다(sound_lesson_i18n 의
  `i18n/ko.json` 금지와 같은 이유).
- 사람이 고친 번역을 넣으면(UPDATE) 요청 경로는 그 행을 그대로 쓴다 — 이미 있는 행은 다시
  번역하지 않는다.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, Identity, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class CurTextI18n(Base, TimestampMixin):
    __tablename__ = "cur_text_i18n"
    __table_args__ = (
        UniqueConstraint("kind", "source", "locale", name="uq_cur_text_i18n"),
        CheckConstraint("locale <> 'ko'", name="ck_cur_text_i18n_not_ko"),
    )

    cur_text_i18n_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(
        Text, nullable=False, comment="무엇의 번역인가 — cur_situation(cur_lesson.situation)",
    )
    source: Mapped[str] = mapped_column(
        Text, nullable=False, comment="한국어 원문 그대로(키) — 원문이 바뀌면 새 행",
    )
    locale: Mapped[str] = mapped_column(
        Text, nullable=False, comment="번역 언어(ISO 639-1, member.language 축) — ko 금지",
    )
    text: Mapped[str] = mapped_column(Text, nullable=False, comment="번역문")
