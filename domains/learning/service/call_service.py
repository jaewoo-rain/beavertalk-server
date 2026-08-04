"""CallService — 통화 일괄 저장 + 조회/평점/삭제.

핵심: 통화 한 건(call + 발화들 + 발화별 평가 + 원본)을 **한 트랜잭션**으로 저장.
평가는 발화별 1:1 — 발화마다 Evaluation 을 만들어 연결(채점 전이면 점수 NULL placeholder).
"""

from __future__ import annotations

import logging
from datetime import date as _date, datetime, time as _time, timedelta, timezone
from typing import Sequence

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.models.evaluation import Evaluation
from domains.learning.models.sentence import Sentence
from core.config import settings
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


logger = logging.getLogger(__name__)


def _avg(values: list) -> float | None:
    nums = [v for v in values if v is not None]
    return round(sum(nums) / len(nums), 1) if nums else None


# '오늘 통화함'의 정의는 CallRepository.has_call_in_window 가 소유한다 —
# **학습자가 최소 한 번 말한** done/analyzing 통화. 옛 DAILY_MIN_CALL_S(10초) 기준은
# 폐기했다(자의적이었고, 마이크가 안 열린 통화가 하루를 소모했다).

# ── 일일 통화 한도 ─────────────────────────────────────────────────────── #
# 콜타입별로 **따로** 센다 — 레벨테스트를 했어도 일반 통화 1회가 남는다.
# 근거: docs/20260729_1243_일일-통화-한도-서버-거절.md
DAILY_CALL_LIMIT: dict[str, int] = {"normal": 1, "level_test": 1}

# 3티어 재편(2026-08-04): 플랜별 한도. 빈 dict = 무제한.
# Free(플랜 없음)만 하루 1회고, Pro·Max 는 무제한이다(기획서 §1).
#
# ⚠ 지금은 **실동작 변화가 없다** — 유료구매가 임시차단돼 유료 회원이 0명이다.
#   결제가 붙는 날 "Pro 결제했는데 하루 1회"로 나가지 않도록 배선만 미리 해둔다.
DAILY_CALL_LIMIT_BY_PLAN: dict[str | None, dict[str, int]] = {
    None: DAILY_CALL_LIMIT,   # Free
    "pro": {},                # 무제한
    "max": {},                # 무제한
}


def daily_window_utc(local_date: _date, tz_offset_min: int) -> tuple[datetime, datetime]:
    """클라 로컬 하루 → UTC 반열린 구간 [start, end).

    서버가 UTC 로 경계를 고정하면 한국 사용자는 오전 9시에 날짜가 바뀐다. 외국인
    학습자라 타임존이 제각각이므로 경계는 클라가 알려준 오프셋으로 잡는다
    (daily_status 와 같은 방식 — 두 곳이 어긋나면 "배지는 안 했다는데 서버는 거절"이 된다).
    """
    offset = timedelta(minutes=tz_offset_min)
    start_utc = (datetime.combine(local_date, _time.min) - offset).replace(
        tzinfo=timezone.utc
    )
    return start_utc, start_utc + timedelta(days=1)


def effective_plan(db: Session, member_id: int) -> str | None:
    """지금 **실제로 혜택이 열려 있는** 플랜. Free 면 None.

    ⛔ 판정 규칙을 여기서 새로 쓰지 않고 commerce 의 resolve_status 를 재사용한다 —
      두 곳이 어긋나면 "앱은 되는데 서버가 거절"이 된다. 특히 grace(결제 재시도 중,
      **접근 유지**)와 on_hold(유예도 끝남, **접근 차단**)의 비대칭이 그렇다.

    실패는 Free 로 떨어뜨린다(R5): 구독 조회가 통화를 막으면 안 되고, 모르면
    보수적으로 무료 한도를 적용하는 편이 과금 사고보다 낫다.
    """
    try:
        from domains.commerce.repository.subscribe_repository import SubscribeRepository
        from domains.commerce.service.subscription_status import resolve_status

        resolved = resolve_status(SubscribeRepository(db).list_by_member(member_id))
    except Exception:  # noqa: BLE001 - 구독 조회 실패가 통화를 막으면 안 된다
        logger.warning("call: 플랜 판정 실패 → Free 로 처리 member=%s", member_id)
        return None
    # 접근이 열리는 상태에서만 플랜을 인정한다. on_hold·expired·free 는 혜택 없음.
    if resolved.state in ("trial", "active_pro", "active_max", "grace", "ending"):
        return resolved.plan
    return None


def is_daily_limit_reached(
    db: Session, member_id: int, call_type: str, tz_offset_min: int = 0
) -> bool:
    """이 회원이 오늘(클라 로컬) 해당 콜타입 한도를 이미 썼는지.

    ⛔ **ENV == "prod" 에서만 적용한다.** 그 외(dev/test/데모)는 자유롭게 쓴다 —
    테스트하다 하루가 잠기면 개발이 안 된다.

    "통화했다"의 정의는 daily_status 와 **같은 것**을 쓴다(학습자 발화 ≥ 1 +
    done/analyzing). 두 곳이 다른 정의를 쓰면 "홈 배지는 안 했다는데 서버는 거절"이 된다.
    마이크가 안 열렸거나 듣기만 한 통화는 한도를 소모하지 않는다.

    남는 구멍: 조용히 5분 세션을 열고 끊으면 한도는 안 깎이는데 Gemini 비용은 나간다.
    한도(상품)와 남용 방지(인프라)는 다른 축이라, 필요해지면 "하루 세션 오픈 N회" 같은
    별도 레이트리밋으로 잡는다.

    tz_offset_min: 클라 UTC 오프셋(분, 동쪽 +). KST=540. 미전송이면 0(UTC).
    """
    if settings.ENV != "prod":
        return False
    limit = DAILY_CALL_LIMIT_BY_PLAN.get(effective_plan(db, member_id), DAILY_CALL_LIMIT).get(
        call_type
    )
    if not limit:
        return False  # 한도가 없는 플랜(유료) 또는 정의되지 않은 콜타입은 막지 않는다
    now_local = datetime.now(timezone.utc) + timedelta(minutes=tz_offset_min)
    start_utc, end_utc = daily_window_utc(now_local.date(), tz_offset_min)
    return CallRepository(db).has_call_in_window(
        member_id, start_utc, end_utc, call_type=call_type
    )


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
            feedback=call.feedback,  # 요구1: 격려 한마디(/result 만 노출)
            rating=call.rating,
            average=average,
            sentences=[CallResultSentence.model_validate(s) for s in active],
        )

    def daily_status(self, member_id: int, local_date: str, tz_offset_min: int) -> dict:
        """'오늘 통화함' 파생 체크 — member 컬럼/일일 초기화 없이 call 에서 계산.

        Args:
            local_date: 클라이언트 로컬 날짜 "YYYY-MM-DD"(사용자가 '오늘'이라 여기는 날).
            tz_offset_min: 클라이언트 UTC 오프셋(분, 동쪽 +). KST=540. 로컬 하루 경계를 UTC 로 환산.

        유효 통화 = 그 로컬 하루 안에 시작 + total_time>=DAILY_MIN_CALL_S + status in(done,analyzing).
        외국인 사용자 타임존이 제각각이라 경계를 서버가 고정하지 않고 클라 로컬 날짜/오프셋으로 받는다.
        """
        try:
            d = _date.fromisoformat(local_date)
        except (ValueError, TypeError):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "date 는 YYYY-MM-DD 형식이어야 합니다."
            )
        if not -14 * 60 <= tz_offset_min <= 14 * 60:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "tz_offset 은 분 단위(-840~840)여야 합니다."
            )
        start_utc, end_utc = daily_window_utc(d, tz_offset_min)
        # 콜타입을 나눠 센다. 한도가 콜타입별로 따로 있으므로(일반 1 + 레벨테스트 1),
        # 합쳐서 세면 레벨테스트만 해도 홈 배지가 "오늘 통화함"이 돼 일반 통화가 아직
        # 남았는데 소진된 것처럼 보인다.
        return {
            "date": local_date,
            "called_today": self.repo.has_call_in_window(
                member_id, start_utc, end_utc, call_type="normal"
            ),
            "level_test_today": self.repo.has_call_in_window(
                member_id, start_utc, end_utc, call_type="level_test"
            ),
        }

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
