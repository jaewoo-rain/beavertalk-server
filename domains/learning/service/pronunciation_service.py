"""발음 상세·이력 서비스 — 집계(순수 함수) + 코칭 LLM(캐시) 오케스트레이션.

T8 `GET /calls/{call_id}/pronunciation`(async): 문장별 점수 + 소리(alpha)별 집계 +
    국가 맞춤 코칭 한마디(PronunciationTip LLM, counted 복습 수 기반 캐시B).
T9 `GET /calls/pronunciation-history`(sync): 최근5 통화의 날짜·문장수·평균 점수.

레이어 규율: 라우터 → 이 서비스 → pronunciation_repository → models. 라우터는 repository 를
직접 부르지 않는다. 쓰기(캐시 저장)는 이 서비스가 `db.commit()`(명시적 커밋). gemini 호출은
async 지만 DB 접근은 `run_db`(threadpool + 짧은 세션)로 감싼다.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from google import genai
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from core import gemini_analysis
from core.config import Settings
from core.persona_prompt import _LOCALE_LABEL
from domains.learning.models.call import Call
from domains.learning.models.review import Review
from domains.learning.models.sentence import Sentence
from domains.learning.repository.pronunciation_repository import PronunciationRepository
from domains.learning.schemas.pronunciation import (
    PronHistoryItem,
    PronSentenceScore,
    PronunciationReport,
    SoundAggregate,
)
from domains.learning.service.normalcall_service import run_db

logger = logging.getLogger(__name__)


class PronunciationTip(BaseModel):
    """국가 맞춤 발음 코칭 한마디(구조화 LLM 출력) — 모국어 1문장."""

    explanation: str


# --------------------------------------------------------------------------- #
# 순수 집계 (DB/LLM 무관 — 단위 테스트 용이)
# --------------------------------------------------------------------------- #
def build_sentence_scores(sentences: Sequence[Sentence]) -> list[PronSentenceScore]:
    """모든 활성 문장 → 공식점수(Evaluation) 직독. 미복습(평가 없음)이면 점수 전부 None."""
    result: list[PronSentenceScore] = []
    for s in sentences:
        ev = s.evaluation
        result.append(
            PronSentenceScore(
                sentence_id=s.sentence_id,
                korean_sentence=s.korean_sentence,
                total_score=ev.total_score if ev else None,
                pronunciation=ev.pronunciation if ev else None,
                fluency=ev.fluency if ev else None,
                rhythm=ev.rhythm if ev else None,
            )
        )
    return result


def aggregate_sounds(last_counted_reviews: Sequence[Review]) -> list[SoundAggregate]:
    """문장별 마지막 counted 복습의 phonemes 만 순회 → alpha 버킷별 집계.

    - attempts        : alpha 출현 횟수
    - passes          : pronunciation >= 80 인 횟수
    - pronunciation_avg: 평균(소수 1자리)
    phonemes 가 없는(옛) 복습은 자연히 제외되며, 자모가 전혀 없으면 빈 리스트를 반환한다.
    (counted 여부는 리포지토리에서 이미 걸러진 입력을 전제한다.)
    """
    buckets: dict[str, dict[str, float]] = {}
    for review in last_counted_reviews:
        feedback = review.feedback if isinstance(review.feedback, dict) else {}
        phonemes = feedback.get("phonemes")
        if not isinstance(phonemes, list):
            continue
        for p in phonemes:
            if not isinstance(p, dict):
                continue
            alpha = p.get("alpha")
            if not alpha:
                continue
            try:
                score = float(p.get("pronunciation", 0) or 0)
            except (TypeError, ValueError):
                score = 0.0
            b = buckets.setdefault(str(alpha), {"attempts": 0, "passes": 0, "sum": 0.0})
            b["attempts"] += 1
            b["sum"] += score
            if score >= 80:
                b["passes"] += 1

    sounds = [
        SoundAggregate(
            alpha=alpha,
            attempts=int(b["attempts"]),
            passes=int(b["passes"]),
            pronunciation_avg=round(b["sum"] / b["attempts"], 1) if b["attempts"] else 0.0,
        )
        for alpha, b in buckets.items()
    ]
    # 안정적 출력 순서(alpha 사전순).
    sounds.sort(key=lambda s: s.alpha)
    return sounds


# --------------------------------------------------------------------------- #
# LLM 코칭 한마디
# --------------------------------------------------------------------------- #
def _tip_prompts(alpha: str, country: Optional[str], language: Optional[str]) -> tuple[str, str]:
    """PronunciationTip 용 (system_instruction, prompt) 조립 — LLM 생성 0, 순수 문자열.

    - country 있음: 해당 국가 화자가 왜 그 자모를 어려워하는지 모국어로 1문장.
    - country 없음: 많은 학습자가 그 자모를 어려워하는 이유 1문장.
    공통: 점수·교정 금지, 따뜻하게 안심시키는 톤, 모국어(language)로 작성.
    """
    lang_label = _LOCALE_LABEL.get(language or "", language or "영어(English)")
    system = (
        "너는 한국어 발음을 따뜻하게 코칭하는 선생님이다. 학습자가 특정 소리를 어려워하는 "
        f"이유를 학습자의 모국어({lang_label})로 딱 1문장만 설명한다. 규칙: "
        "① 점수·수치·급을 언급하지 마라. ② 교정 지시('이렇게 발음해라')를 하지 마라. "
        "③ 누구나 그럴 수 있다고 안심시키는 격려 톤으로. ④ 반드시 모국어로, 1문장으로. "
        "출력은 JSON(explanation)만."
    )
    if country:
        prompt = (
            f"{country} 화자가 한국어 자모 '{alpha}' 발음을 왜 어려워하는지, "
            f"{lang_label}로 따뜻하게 1문장으로 설명해줘."
        )
    else:
        prompt = (
            f"많은 학습자가 한국어 자모 '{alpha}' 발음을 왜 어려워하는지, "
            f"{lang_label}로 따뜻하게 1문장으로 설명해줘."
        )
    return system, prompt


def _save_pron_cache(db: Session, call_id: int, comment: str, n: int) -> None:
    """LLM 코칭 결과를 call.pron_feedback / pron_feedback_n 에 캐시(명시적 커밋)."""
    call = db.get(Call, call_id)
    if call is None:
        return
    call.pron_feedback = comment
    call.pron_feedback_n = n
    db.commit()


async def _resolve_comment(
    *,
    call_id: int,
    sounds: list[SoundAggregate],
    counted_n: int,
    cached_comment: Optional[str],
    cached_n: Optional[int],
    country: Optional[str],
    language: Optional[str],
    session_factory: sessionmaker,
    client: Optional[genai.Client],
    settings: Settings,
) -> Optional[str]:
    """코칭 한마디 결정 — 자모없음/캐시적중/LLM/폴백 분기.

    - 자모 없음(sounds 빈): comment=None, LLM 스킵.
    - 캐시 적중(pron_feedback_n == 현재 counted 수): 기존 pron_feedback 재사용.
    - 그 외: 최저 pronunciation_avg alpha 1개로 PronunciationTip LLM 호출 → 성공 시 캐시 저장.
    - client None / LLM 실패: 기존 캐시(또는 None)로 graceful 폴백.
    """
    if not sounds:
        return None
    if cached_n is not None and cached_n == counted_n:
        return cached_comment
    if client is None:
        return cached_comment

    worst = min(sounds, key=lambda s: s.pronunciation_avg)
    system, prompt = _tip_prompts(worst.alpha, country, language)
    tip = await gemini_analysis.generate_structured(
        client,
        settings.JUDGE_MODEL,
        system_instruction=system,
        prompt=prompt,
        schema=PronunciationTip,
        thinking_budget=0,
    )
    if tip is None or not tip.explanation.strip():
        return cached_comment  # LLM 실패 → 기존 캐시 또는 None

    comment = tip.explanation.strip()
    await run_db(session_factory, lambda db: _save_pron_cache(db, call_id, comment, counted_n))
    return comment


# --------------------------------------------------------------------------- #
# T8 오케스트레이션 (async — 라우터에서 1회 await)
# --------------------------------------------------------------------------- #
class _PronGathered:
    """run_db 안(세션 열림)에서 집계까지 끝낸 순수 데이터 — 세션 밖 안전 전달용."""

    def __init__(
        self,
        *,
        sentence_scores: list[PronSentenceScore],
        sounds: list[SoundAggregate],
        country: Optional[str],
        language: Optional[str],
        counted_n: int,
        cached_comment: Optional[str],
        cached_n: Optional[int],
    ) -> None:
        self.sentence_scores = sentence_scores
        self.sounds = sounds
        self.country = country
        self.language = language
        self.counted_n = counted_n
        self.cached_comment = cached_comment
        self.cached_n = cached_n


def _gather(db: Session, call_id: int, member_id: int) -> Optional[_PronGathered]:
    """소유 확인 + 문장/집계/국가/캐시 스냅샷을 세션 안에서 모두 계산(ORM 누수 방지)."""
    repo = PronunciationRepository(db)
    call = repo.get_owned_call(call_id, member_id)
    if call is None:
        return None
    sentences = repo.get_active_sentences(call_id)
    last_reviews = repo.get_last_counted_reviews(call_id)
    return _PronGathered(
        sentence_scores=build_sentence_scores(sentences),
        sounds=aggregate_sounds(last_reviews),
        country=repo.get_first_country(member_id),
        language=repo.get_member_language(member_id),
        counted_n=repo.count_counted_reviews(call_id),
        cached_comment=call.pron_feedback,
        cached_n=call.pron_feedback_n,
    )


async def get_pronunciation_report(
    *,
    call_id: int,
    member_id: int,
    session_factory: sessionmaker,
    client: Optional[genai.Client],
    settings: Settings,
) -> Optional[PronunciationReport]:
    """T8 통화별 발음 상세 — DB 집계(run_db) → 코칭 한마디 → 응답 조립.

    Returns:
        PronunciationReport, 또는 없거나 타인 통화면 None(라우터가 404).
    """
    gathered = await run_db(session_factory, lambda db: _gather(db, call_id, member_id))
    if gathered is None:
        return None

    comment = await _resolve_comment(
        call_id=call_id,
        sounds=gathered.sounds,
        counted_n=gathered.counted_n,
        cached_comment=gathered.cached_comment,
        cached_n=gathered.cached_n,
        country=gathered.country,
        language=gathered.language,
        session_factory=session_factory,
        client=client,
        settings=settings,
    )
    return PronunciationReport(
        call_id=call_id,
        country=gathered.country,
        sentences=gathered.sentence_scores,
        sounds=gathered.sounds,
        comment=comment,
    )


# --------------------------------------------------------------------------- #
# T9 최근5 이력 (sync — LLM 없음, 얇게)
# --------------------------------------------------------------------------- #
def get_pronunciation_history(db: Session, member_id: int) -> list[PronHistoryItem]:
    """최근5 통화(normal·done)의 [날짜, 활성 문장수, counted 문장 total_score 평균]."""
    repo = PronunciationRepository(db)
    calls = repo.recent_calls(member_id, limit=5)
    call_ids = [c.call_id for c in calls]
    counts = repo.active_sentence_counts(call_ids)
    avgs = repo.counted_score_avgs(call_ids)
    items: list[PronHistoryItem] = []
    for c in calls:
        avg = avgs.get(c.call_id)
        items.append(
            PronHistoryItem(
                call_id=c.call_id,
                call_date=c.call_date,
                sentence_count=counts.get(c.call_id, 0),
                score=round(avg, 1) if avg is not None else None,
            )
        )
    return items
