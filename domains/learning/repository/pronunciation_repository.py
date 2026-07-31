"""발음 상세·이력 리포지토리 — 순수 SELECT(쿼리만, commit 금지).

T8(통화별 발음 상세)·T9(최근5 발음 이력)에 필요한 조회를 담당한다. 소유 검증·문장/평가
로딩·문장별 마지막 counted 복습·국가/모국어·이력 집계까지 여기서 SELECT 만 수행하고,
집계 로직(alpha 버킷·평균)과 LLM 은 service 가 소유한다.

⚠️ 이식성: 계획서의 "문장별 마지막 counted 복습"은 PostgreSQL `DISTINCT ON` 을 가정하나,
테스트(sqlite)·양 DB 호환을 위해 정렬 후 문장별 첫 행만 취하는 방식으로 구현한다
(sentence_id, created_at DESC, review_id DESC 정렬 → 문장당 첫 행 = 최신 counted 복습).
"""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session, joinedload

from domains.account.models.member import Member
from domains.learning.models.call import Call
from domains.learning.models.evaluation import Evaluation
from domains.learning.models.review import Review
from domains.learning.models.sentence import Sentence


class PronunciationRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ── T8: 통화별 발음 상세 ────────────────────────────────────────────── #
    def get_owned_call(self, call_id: int, member_id: int) -> Optional[Call]:
        """소유 통화 확인 — 없거나 타인 통화면 None(라우터가 404 로 매핑)."""
        call = self.db.get(Call, call_id)
        if call is None or call.member_id != member_id:
            return None
        return call

    def get_active_sentences(self, call_id: int) -> Sequence[Sentence]:
        """활성 문장(deleted_at NULL) + 평가(1:1) joinedload — 문장별 점수 직독용."""
        stmt = (
            select(Sentence)
            .where(Sentence.call_id == call_id, Sentence.deleted_at.is_(None))
            .options(joinedload(Sentence.evaluation))
            .order_by(Sentence.sentence_id)
        )
        return self.db.scalars(stmt).all()

    def get_last_counted_reviews(self, call_id: int) -> list[Review]:
        """활성 문장별 '마지막 counted 복습' 1건씩 — 소리 집계의 원천.

        counted 복습만 정렬(문장 → 최신순)해 문장당 첫 행만 취한다.
        phonemes 는 review.feedback JSON 에 있으므로 별도 로딩 없이 스칼라로 읽힌다.
        """
        stmt = (
            select(Review)
            .join(Sentence, Review.sentence_id == Sentence.sentence_id)
            .where(
                Sentence.call_id == call_id,
                Sentence.deleted_at.is_(None),
                Review.counted.is_(True),
            )
            .order_by(
                Review.sentence_id,
                Review.created_at.desc(),
                Review.review_id.desc(),
            )
        )
        rows = self.db.scalars(stmt).all()
        last_per_sentence: list[Review] = []
        seen: set[int] = set()
        for review in rows:
            if review.sentence_id in seen:
                continue
            seen.add(review.sentence_id)
            last_per_sentence.append(review)
        return last_per_sentence

    def count_counted_reviews(self, call_id: int) -> int:
        """이 통화 활성 문장에 달린 counted 복습 총수 — LLM 캐시 무효화키(N)."""
        stmt = (
            select(func.count())
            .select_from(Review)
            .join(Sentence, Review.sentence_id == Sentence.sentence_id)
            .where(
                Sentence.call_id == call_id,
                Sentence.deleted_at.is_(None),
                Review.counted.is_(True),
            )
        )
        return int(self.db.scalar(stmt) or 0)

    def get_first_country(self, member_id: int) -> Optional[str]:
        """회원 억양(speak_country)의 1순위 국가 이름 — 없으면 None."""
        member = self.db.get(Member, member_id, options=[joinedload(Member.speak_country)])
        if member is None or member.speak_country is None:
            return None
        return member.speak_country.first_country

    def get_member_language(self, member_id: int) -> Optional[str]:
        """회원 모국어(locale) — LLM 코칭 언어."""
        member = self.db.get(Member, member_id)
        return member.language if member else None

    # ── T9: 최근5 발음 이력 ─────────────────────────────────────────────── #
    def recent_calls(self, member_id: int, limit: int = 5) -> Sequence[Call]:
        """일반(normal)·완료(done) 통화 최근순 N건 — 이력 카드용(발화 미로딩)."""
        stmt = (
            select(Call)
            .where(
                Call.member_id == member_id,
                Call.call_type == "normal",
                Call.status == "done",
            )
            .order_by(Call.call_date.desc(), Call.call_id.desc())
            .limit(limit)
        )
        return self.db.scalars(stmt).all()

    def scored_call_ids(self, member_id: int, limit: int) -> list[int]:
        """발음 점수가 **실제로 있는** 최근 통화 id N개(최근순).

        recent_calls 와 달리 점수 없는 통화를 건너뛴다. 마이페이지의 "최근 N세션
        평균"은 점수가 있는 세션 N개를 뜻하기 때문 — 통화만 하고 발음 챌린지를
        안 누른 통화(대부분이다)를 세면 표본이 순식간에 비어버린다.
        """
        stmt = (
            select(Call.call_id)
            .join(Sentence, Sentence.call_id == Call.call_id)
            .join(Evaluation, Evaluation.sentence_id == Sentence.sentence_id)
            .where(
                Call.member_id == member_id,
                Call.call_type == "normal",
                Call.status == "done",
                Sentence.deleted_at.is_(None),
                Evaluation.pronunciation.is_not(None),
            )
            .group_by(Call.call_id, Call.call_date)
            .order_by(Call.call_date.desc(), Call.call_id.desc())
            .limit(limit)
        )
        return [int(r) for r in self.db.scalars(stmt).all()]

    def metric_avgs(self, call_ids: Sequence[int]) -> dict[str, Optional[float]]:
        """주어진 통화들의 발음 4지표 평균 + 표본 수.

        통화별로 먼저 평균 내지 않고 **문장 단위로 한 번에** 평균한다. 통화별 평균의
        평균은 문장이 1개인 통화에 과한 가중치를 준다(통화 A 1문장 50점 + 통화 B
        10문장 90점 → 통화평균 70 vs 문장평균 86.4). 화면 문구가 "세션 평균"이라
        헷갈리지만, 사용자가 기대하는 건 "내 발음이 대체로 몇 점이냐"다.
        """
        empty: dict[str, Optional[float]] = {
            "total_score": None, "pronunciation": None,
            "fluency": None, "rhythm": None, "sentence_count": 0,
        }
        if not call_ids:
            return empty
        stmt = (
            select(
                func.avg(Evaluation.total_score),
                func.avg(Evaluation.pronunciation),
                func.avg(Evaluation.fluency),
                func.avg(Evaluation.rhythm),
                func.count(),
            )
            .join(Sentence, Sentence.sentence_id == Evaluation.sentence_id)
            .where(
                Sentence.call_id.in_(call_ids),
                Sentence.deleted_at.is_(None),
                Evaluation.pronunciation.is_not(None),
            )
        )
        row = self.db.execute(stmt).one_or_none()
        if row is None:
            return empty
        total, pron, flu, rhy, n = row
        return {
            "total_score": float(total) if total is not None else None,
            "pronunciation": float(pron) if pron is not None else None,
            "fluency": float(flu) if flu is not None else None,
            "rhythm": float(rhy) if rhy is not None else None,
            "sentence_count": int(n or 0),
        }

    def active_sentence_counts(self, call_ids: Sequence[int]) -> dict[int, int]:
        """call_id → 활성 문장 수."""
        if not call_ids:
            return {}
        stmt = (
            select(Sentence.call_id, func.count())
            .where(Sentence.call_id.in_(call_ids), Sentence.deleted_at.is_(None))
            .group_by(Sentence.call_id)
        )
        return {cid: int(cnt) for cid, cnt in self.db.execute(stmt).all()}

    def counted_score_avgs(self, call_ids: Sequence[int]) -> dict[int, float]:
        """call_id → counted 복습이 있는 활성 문장들의 total_score 평균.

        평균 대상 = 활성 문장 중 counted 복습을 1건 이상 가진 문장의 Evaluation.total_score.
        해당 문장이 없으면 키 자체가 빠진다(service 가 None 처리).
        """
        if not call_ids:
            return {}
        stmt = (
            select(Sentence.call_id, func.avg(Evaluation.total_score))
            .join(Evaluation, Evaluation.sentence_id == Sentence.sentence_id)
            .where(
                Sentence.call_id.in_(call_ids),
                Sentence.deleted_at.is_(None),
                Evaluation.total_score.is_not(None),
                exists().where(
                    and_(
                        Review.sentence_id == Sentence.sentence_id,
                        Review.counted.is_(True),
                    )
                ),
            )
            .group_by(Sentence.call_id)
        )
        return {
            cid: float(avg)
            for cid, avg in self.db.execute(stmt).all()
            if avg is not None
        }
