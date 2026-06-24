"""CallService — 통화 일괄 저장 + 조회/평점/삭제.

핵심: 통화 한 건(call + 발화들 + 발화별 평가 + 원본)을 **한 트랜잭션**으로 저장.
평가는 발화별 1:1 — 발화마다 Evaluation 을 만들어 연결(채점 전이면 점수 NULL placeholder).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.models.evaluation import Evaluation
from domains.learning.models.sentence import Sentence
from domains.learning.repository.call_repository import CallRepository
from domains.learning.schemas.call import (
    CallCharacterBrief,
    CallCreate,
    CallDetail,
    CallResult,
    CallResultSentence,
    CallSummary,
    EvaluationOut,
    RawDataOut,
    ScoreAverage,
    SentenceOut,
)


def _avg(values: list) -> float | None:
    nums = [v for v in values if v is not None]
    return round(sum(nums) / len(nums), 1) if nums else None


class CallService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = CallRepository(db)

    def create_call(self, member_id: int, data: CallCreate) -> CallDetail:
        call = Call(
            member_id=member_id,
            character_id=data.character_id,
            call_date=data.call_date or datetime.now(timezone.utc),
            total_time=data.total_time,
            summary=data.summary,
            rating=data.rating,
            # 발화별로 Evaluation 을 만들어 연결(1:1). 점수 없으면 placeholder.
            sentences=[
                Sentence(
                    korean_sentence=s.korean_sentence,
                    native_sentence=s.native_sentence,
                    locale=s.locale,
                    voice_url=s.voice_url,
                    is_bookmarked=s.is_bookmarked,
                    evaluation=Evaluation(
                        total_score=s.evaluation.total_score,
                        pronunciation=s.evaluation.pronunciation,
                        fluency=s.evaluation.fluency,
                        rhythm=s.evaluation.rhythm,
                    ),
                )
                for s in data.sentences
            ],
            raw_data=[
                CallRawData(content=r.content, voice_url=r.voice_url, total_time=r.total_time)
                for r in data.raw_data
            ],
        )
        self.repo.add(call)
        self.db.commit()  # call + sentences + evaluations + raw_data 한 트랜잭션
        # 상세 응답을 위해 연관 로딩된 형태로 다시 조회
        return self.get_call(member_id, call.call_id)

    def list_calls(self, member_id: int, limit: int = 20, offset: int = 0) -> list[CallSummary]:
        return [self._to_summary(c) for c in self.repo.list_by_member(member_id, limit, offset)]

    def get_call(self, member_id: int, call_id: int) -> CallDetail:
        call = self.repo.get_detail(call_id)
        self._assert_owner(call, member_id)
        active = [s for s in call.sentences if s.deleted_at is None]  # 소프트 삭제 제외
        return CallDetail(
            **self._summary_fields(call),
            sentences=[self._to_sentence(s) for s in active],
        )

    def get_call_result(self, member_id: int, call_id: int) -> CallResult:
        """통화 종료 후 결과 — 발화 평가들의 평균 + 사용된 문장 전체."""
        call = self.repo.get_detail(call_id)
        self._assert_owner(call, member_id)
        active = [s for s in call.sentences if s.deleted_at is None]  # 소프트 삭제 제외
        evals = [s.evaluation for s in active if s.evaluation]
        average = ScoreAverage(
            total_score=_avg([e.total_score for e in evals]),
            pronunciation=_avg([e.pronunciation for e in evals]),
            fluency=_avg([e.fluency for e in evals]),
            rhythm=_avg([e.rhythm for e in evals]),
        )
        return CallResult(
            call_id=call.call_id,
            summary=call.summary,
            rating=call.rating,
            average=average,
            sentences=[CallResultSentence.model_validate(s) for s in active],
        )

    def get_raw(self, member_id: int, call_id: int) -> list[RawDataOut]:
        call = self.repo.get_with_raw(call_id)
        self._assert_owner(call, member_id)
        return [RawDataOut.model_validate(r) for r in call.raw_data]

    def update_rating(self, member_id: int, call_id: int, rating: int) -> CallSummary:
        call = self.repo.get_detail(call_id)
        self._assert_owner(call, member_id)
        call.rating = rating
        self.db.commit()
        self.db.refresh(call)
        return self._to_summary(call)

    def delete_call(self, member_id: int, call_id: int) -> None:
        call = self.repo.get_basic(call_id)
        self._assert_owner(call, member_id)
        self.repo.delete(call)  # sentences/raw/evaluation 은 CASCADE
        self.db.commit()

    # ── 내부 ──
    def _assert_owner(self, call: Call | None, member_id: int) -> None:
        if call is None or call.member_id != member_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다.")

    def _summary_fields(self, call: Call) -> dict:
        return dict(
            call_id=call.call_id,
            call_date=call.call_date,
            total_time=call.total_time,
            summary=call.summary,
            rating=call.rating,
            character=CallCharacterBrief(
                character_id=call.character.character_id,
                name=call.character.name,
                image_url=call.character.image_url,
            ),
        )

    def _to_summary(self, call: Call) -> CallSummary:
        return CallSummary(**self._summary_fields(call))

    def _to_sentence(self, s: Sentence) -> SentenceOut:
        return SentenceOut(
            sentence_id=s.sentence_id,
            korean_sentence=s.korean_sentence,
            native_sentence=s.native_sentence,
            locale=s.locale,
            voice_url=s.voice_url,
            is_bookmarked=s.is_bookmarked,
            evaluation=EvaluationOut.model_validate(s.evaluation) if s.evaluation else None,
        )
