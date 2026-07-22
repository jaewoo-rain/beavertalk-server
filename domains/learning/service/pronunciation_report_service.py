"""발음 리포트 어댑터 — main 의 발음/국적 기능을 Flutter LearningSummary 로 가공.

main(pronunciation_service)이 이미 실데이터를 낸다:
    - get_pronunciation_report → {country, sentences[점수], sounds[alpha 집계], comment(국가 코칭)}
    - get_pronunciation_history → 최근5 {call_date, sentence_count, score}
여기서는 그걸 받아 Flutter LearningSummary(= LearningSummaryOut) 가 요구하는
통과수·평균·가장 어려웠던 소리·소리별 정확도(2+2 선별)·최근 세션(delta/라벨) 만 얹는다.
즉 목·자체 LLM 은 없고, 국적/자모/코칭은 pronunciation_service 실데이터를 그대로 쓴다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, sessionmaker

from domains.learning.models.call import Call
from domains.learning.schemas.pronunciation import PronHistoryItem, PronunciationReport
from domains.learning.schemas.pronunciation_report import (
    LearningSummaryOut,
    PhonemeStatOut,
    SentenceScoreOut,
    SessionPointOut,
)
from domains.learning.service import pronunciation_service as pron_svc
from domains.learning.service.normalcall_service import run_db

_PASS_THRESHOLD = 80  # 문장 통과 기준(total_score ≥ 80)


def _accuracy(p: PhonemeStatOut) -> int:
    return round(p.correct / p.attempts * 100) if p.attempts else 0


def _select_phonemes(pool: list[PhonemeStatOut]) -> list[PhonemeStatOut]:
    """소리별 정확도 표: 정확도 낮은 2개 → 시도 많은 2개(중복 제외, 최대 4행)."""
    by_acc = sorted(pool, key=lambda p: (_accuracy(p), -p.attempts))
    lowest = by_acc[:2]
    lowest_sounds = {p.sound for p in lowest}
    by_att = sorted(
        (p for p in pool if p.sound not in lowest_sounds),
        key=lambda p: -p.attempts,
    )
    return lowest + by_att[:2]


def _hardest(pool: list[PhonemeStatOut]) -> PhonemeStatOut | None:
    """가장 어려웠던 소리 = 정확도 최저(동률이면 시도 많은 것)."""
    return sorted(pool, key=lambda p: (_accuracy(p), -p.attempts))[0] if pool else None


def _phonemes_from_sounds(report: PronunciationReport) -> list[PhonemeStatOut]:
    """main sounds[{alpha, attempts, passes}] → PhonemeStatOut(정확발음=passes)."""
    return [
        PhonemeStatOut(sound=s.alpha, attempts=s.attempts, correct=s.passes)
        for s in report.sounds
    ]


def _sessions_from_history(history: list[PronHistoryItem]) -> list[SessionPointOut]:
    """최근 세션(oldest first) — 라벨(오늘/M/D)·날짜(M/D)·delta 조립."""
    items = list(reversed(history))  # get_pronunciation_history 는 최신순 → 오래된순으로
    today = datetime.now(timezone.utc).date()
    out: list[SessionPointOut] = []
    prev: int | None = None
    for h in items:
        d = h.call_date or datetime.now(timezone.utc)
        score = round(h.score) if h.score is not None else 0
        out.append(
            SessionPointOut(
                label="오늘" if d.date() == today else f"{d.month}/{d.day}",
                date=f"{d.month}/{d.day}",
                sentences=h.sentence_count,
                score=score,
                delta=None if prev is None else score - prev,
            )
        )
        prev = score
    return out


def _call_date(db: Session, call_id: int) -> datetime | None:
    call = db.get(Call, call_id)
    return call.call_date if call is not None else None


async def build_learning_summary(
    member_id: int,
    call_id: int,
    *,
    session_factory: sessionmaker,
    client: "Any | None",
    settings: "Any",
) -> LearningSummaryOut:
    """main 발음 리포트 + 이력을 LearningSummary 로 가공. 없는 통화면 404."""
    report = await pron_svc.get_pronunciation_report(
        call_id=call_id,
        member_id=member_id,
        session_factory=session_factory,
        client=client,
        settings=settings,
    )
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다.")

    history = await run_db(
        session_factory, lambda db: pron_svc.get_pronunciation_history(db, member_id)
    )
    call_date = await run_db(session_factory, lambda db: _call_date(db, call_id))

    # ── 문장별 + 통과·평균(실데이터, 미복습 점수는 0) ──
    sentences = [
        SentenceScoreOut(
            sentence=s.korean_sentence or "",
            pronunciation=s.pronunciation or 0,
            fluency=s.fluency or 0,
            rhythm=s.rhythm or 0,
        )
        for s in report.sentences
    ]
    totals = [s.total_score for s in report.sentences if s.total_score is not None]
    prons = [s.pronunciation for s in report.sentences if s.pronunciation is not None]
    flus = [s.fluency for s in report.sentences if s.fluency is not None]
    rhys = [s.rhythm for s in report.sentences if s.rhythm is not None]
    total_n = len(report.sentences)
    passed = sum(1 for t in totals if t >= _PASS_THRESHOLD)

    def _avg(xs: list[int]) -> int:
        return round(sum(xs) / len(xs)) if xs else 0

    # ── 소리별 정확도(실 alpha 집계) + 가장 어려웠던 소리 ──
    pool = _phonemes_from_sounds(report)
    hardest = _hardest(pool)
    hardest_sound = hardest.sound if hardest else ""
    hardest_evidence = (
        f"{hardest.sound}에서 {hardest.attempts}번 중 "
        f"{hardest.attempts - hardest.correct}번 새어 나갔어요"
        if hardest
        else ""
    )

    return LearningSummaryOut(
        passed=passed,
        total=total_n,
        date=call_date or datetime.now(timezone.utc),
        overall=_avg(totals),
        pronunciation=_avg(prons),
        fluency=_avg(flus),
        rhythm=_avg(rhys),
        hardest_sound=hardest_sound,
        hardest_evidence=hardest_evidence,
        l1_interference=report.comment or "",  # main 의 국가 맞춤 코칭(LLM)
        phonemes=_select_phonemes(pool),
        sentences=sentences,
        sessions=_sessions_from_history(history),
    )
