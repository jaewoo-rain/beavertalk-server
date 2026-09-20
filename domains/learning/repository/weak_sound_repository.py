"""취약 발음 학습 리포지토리 — 순수 DB 접근(쿼리만, commit 금지).

목록·과·점수·국가 통계 조회 + 점수 행 upsert 를 담당한다. 집계·추천·점수 산식은 service
가 소유한다(레이어 규율).

`recent_counted_reviews` 만 설명이 필요하다 — pronunciation_repository 의 동명 메서드는
**통화 1건 안에서** 문장별 마지막 복습을 고른다. 여기는 **회원의 최근 통화 여러 건**을
한꺼번에 본다. 취약 발음 목록은 통화 1건이 아니라 「요즘 내 발음」이기 때문이다.
"""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from domains.learning.models.call import Call
from domains.learning.models.member_sound_score import MemberSoundScore
from domains.learning.models.national_sound_stat import NationalSoundStat
from domains.learning.models.review import Review
from domains.learning.models.sentence import Sentence
from domains.learning.models.sound_lesson import SoundLesson


class WeakSoundRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ── 콘텐츠 마스터 ───────────────────────────────────────────────────── #
    def get_lessons(self) -> Sequence[SoundLesson]:
        """전체 학습 과(정렬 순서). 30행이라 통째로 읽어도 무해하다."""
        return self.db.scalars(
            select(SoundLesson).order_by(SoundLesson.sort_order, SoundLesson.sound_lesson_id)
        ).all()

    def get_lesson(self, sound_key: str) -> Optional[SoundLesson]:
        return self.db.scalar(select(SoundLesson).where(SoundLesson.sound_key == sound_key))

    # ── 국적별 통계 ─────────────────────────────────────────────────────── #
    def get_national_stats(self, country_iso: str) -> Sequence[NationalSoundStat]:
        """ISO 2자리로 취약 소리 목록 — rank 오름차순. 없는 나라면 빈 시퀀스.

        **이름이 아니라 ISO 로 찾는다.** speak_country 에는 영문명만 저장되지만,
        그 이름과 이 표의 이름이 갈리는 경우가 실제로 있었다(Russia vs Russian
        Federation, 2026-09-21 실측). 이름 대조는 틀려도 예외가 아니라 빈 목록이라
        아무도 모른 채 그 나라 사용자만 국적 섹션을 못 본다. 이름→ISO 변환은
        `core.nationality.iso_for_country` 가 맡고, 여기는 ISO 만 받는다.
        """
        return self.db.scalars(
            select(NationalSoundStat)
            .where(NationalSoundStat.country_iso == country_iso)
            .order_by(NationalSoundStat.rank, NationalSoundStat.sound_key)
        ).all()

    # ── 회원 점수 ───────────────────────────────────────────────────────── #
    def get_scores(self, member_id: int) -> dict[str, MemberSoundScore]:
        rows = self.db.scalars(
            select(MemberSoundScore).where(MemberSoundScore.member_id == member_id)
        ).all()
        return {r.sound_key: r for r in rows}

    def get_score(self, member_id: int, sound_key: str) -> Optional[MemberSoundScore]:
        return self.db.scalar(
            select(MemberSoundScore).where(
                MemberSoundScore.member_id == member_id,
                MemberSoundScore.sound_key == sound_key,
            )
        )

    def add_score(self, row: MemberSoundScore) -> MemberSoundScore:
        """신규 점수 행 추가(commit 은 service 가 한다)."""
        self.db.add(row)
        return row

    # ── 내 발음 원천(복습 집계) ─────────────────────────────────────────── #
    def recent_counted_reviews(self, member_id: int, call_limit: int = 5) -> list[Review]:
        """최근 통화 N건의 '문장별 마지막 counted 복습' — 내 취약 발음 집계의 원천.

        DISTINCT ON 을 쓰지 않는다(sqlite 테스트 호환). 정렬 후 문장당 첫 행만 취하는
        pronunciation_repository 와 같은 방식이다.
        """
        call_ids = [
            int(c) for c in self.db.scalars(
                select(Call.call_id)
                .where(Call.member_id == member_id, Call.status == "done")
                .order_by(Call.call_date.desc(), Call.call_id.desc())
                .limit(call_limit)
            ).all()
        ]
        if not call_ids:
            return []
        rows = self.db.scalars(
            select(Review)
            .join(Sentence, Review.sentence_id == Sentence.sentence_id)
            .where(
                Sentence.call_id.in_(call_ids),
                Sentence.deleted_at.is_(None),
                Review.counted.is_(True),
            )
            .order_by(
                Review.sentence_id,
                Review.created_at.desc(),
                Review.review_id.desc(),
            )
        ).all()
        out: list[Review] = []
        seen: set[int] = set()
        for review in rows:
            if review.sentence_id in seen:
                continue
            seen.add(review.sentence_id)
            out.append(review)
        return out
