"""발음 리포트 서비스 — 복습 종료 후 화면(Flutter LearningSummary) 데이터 조립.

레이어 규율: routers → service → repository. 이 서비스는 통화·평가를 집계해
PronunciationReport 를 만든다(읽기 전용, 커밋 없음).

현 단계:
    - 문장별 결과 / 최근 세션 / 통과·평균 = 실데이터(통화·Evaluation 집계).
      평가 점수가 아직 없으면(복습 미완/키 부재) 결정적 목값으로 폴백 → 화면은 항상 그려짐.
    - 소리별 정확도(phonemes) / 가장 어려웠던 소리 / L1 간섭 = **목**(음소 채점 모델 미구현).
      음소 모델(SpeechSuper 호환 출력)이 붙으면 이 부분만 실집계로 교체한다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload, selectinload

from core.config import settings
from domains.learning.models.call import Call
from domains.learning.models.sentence import Sentence
from domains.learning.schemas.pronunciation_report import (
    PhonemeStatOut,
    PronunciationReport,
    SentenceScoreOut,
    SessionPointOut,
)

_PASS_THRESHOLD = 80  # 문장 통과 기준(total_score ≥ 80)
_RECENT_SESSIONS = 5  # 최근 세션 개수

# ── 음소 채점 모델 미구현 → 목 풀(모델 붙으면 이 풀만 실집계로 교체) ──
# 아래 선별·hardest·evidence 로직은 실데이터에도 그대로 적용된다.
_MOCK_PHONEME_POOL = [
    PhonemeStatOut(sound="받침 ㄹ", attempts=7, correct=3),          # 정확도 43
    PhonemeStatOut(sound="받침 ㄱ", attempts=10, correct=6),         # 60
    PhonemeStatOut(sound="ㅐ / ㅔ 구분", attempts=8, correct=5),      # 63
    PhonemeStatOut(sound="된소리(ㄲ·ㄸ·ㅃ)", attempts=6, correct=4),   # 67
    PhonemeStatOut(sound="ㅓ / ㅗ 구분", attempts=12, correct=9),     # 75
    PhonemeStatOut(sound="받침 ㅇ", attempts=9, correct=8),          # 89
    PhonemeStatOut(sound="ㅅ / ㅆ 구분", attempts=5, correct=5),      # 100
]
# L1 간섭 설명 = LLM/feedback.json 이 채울 자리(현재 목).
_MOCK_L1_INTERFERENCE = "이 받침 소리는 모국어에 없어서 어려워요. 당신 잘못이 아니에요."


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


logger = logging.getLogger(__name__)


async def _generate_l1_feedback(
    client: "Any | None", native_locale: str | None, hardest: PhonemeStatOut | None
) -> str:
    """L1 간섭 피드백을 LLM 으로 생성(학습자 모국어). 실패/미구성이면 목 폴백.

    가장 어려웠던 소리 + 시도/오답 수 + 학습자 모국어(locale)를 주고, 왜 어려운지(모국어
    간섭) + 격려를 2문장으로 받는다. feedback.json(국적별 정적 데이터)로 교체 가능.
    """
    if client is None or hardest is None:
        return _MOCK_L1_INTERFERENCE
    system = (
        "너는 한국어 발음 코치다. 학습자에게 줄 격려를 딱 한 문장으로, 짧고 따뜻하게 "
        "학습자의 모국어로만 써라. 머리말·따옴표·설명 없이 한 문장만."
    )
    prompt = (
        f"학습자 모국어 locale='{native_locale or 'en'}'. "
        f"한국어 소리 '{hardest.sound}'가 이 학습자에게 어려웠다. "
        "이 소리가 모국어에 없어 어려운 건 네 잘못이 아니라는 뉘앙스로, "
        "따뜻한 격려 한 문장만 학습자 모국어로 써라."
    )
    try:
        from google.genai import types

        resp = await client.aio.models.generate_content(
            model=settings.JUDGE_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system, temperature=0.7
            ),
        )
        text = (getattr(resp, "text", None) or "").strip()
        return text or _MOCK_L1_INTERFERENCE
    except Exception as exc:  # noqa: BLE001 - LLM 실패 graceful
        logger.warning("l1 feedback LLM 실패(목 폴백): %s", exc)
        return _MOCK_L1_INTERFERENCE


def _mock_score(text: str | None, salt: int) -> int:
    """평가 점수가 없을 때 쓰는 결정적 목값(60~100). 같은 문장이면 항상 같은 값."""
    base = sum(ord(c) for c in (text or "x"))
    return 60 + (base + salt) % 41


def _sentence_scores(s: Sentence) -> tuple[int, int, int, int]:
    """(pronunciation, fluency, rhythm, total) — 평가 있으면 실값, 없으면 목값."""
    ev = s.evaluation
    text = s.korean_sentence
    pron = ev.pronunciation if (ev and ev.pronunciation is not None) else _mock_score(text, 1)
    flu = ev.fluency if (ev and ev.fluency is not None) else _mock_score(text, 2)
    rhy = ev.rhythm if (ev and ev.rhythm is not None) else _mock_score(text, 3)
    total = (
        ev.total_score
        if (ev and ev.total_score is not None)
        else round((pron + flu + rhy) / 3)
    )
    return pron, flu, rhy, total


def _active_sentences(call: Call) -> list[Sentence]:
    return [s for s in call.sentences if s.deleted_at is None]


def _call_score(call: Call) -> tuple[int, int]:
    """(문장 수, 세션 점수) — 세션 점수 = 문장 total 평균."""
    active = _active_sentences(call)
    if not active:
        return 0, 0
    totals = [_sentence_scores(s)[3] for s in active]
    return len(active), round(sum(totals) / len(totals))


class PronunciationReportService:
    def __init__(self, db: Session) -> None:
        self.db = db

    async def get_report(
        self,
        member_id: int,
        call_id: int,
        *,
        client: "Any | None" = None,
        native_locale: str | None = None,
    ) -> PronunciationReport:
        call = self.db.get(
            Call,
            call_id,
            options=[
                joinedload(Call.character),
                selectinload(Call.sentences).joinedload(Sentence.evaluation),
            ],
        )
        if call is None or call.member_id != member_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다.")

        active = _active_sentences(call)

        # ── 문장별 결과(실값-or-목) ──
        sentences: list[SentenceScoreOut] = []
        totals: list[int] = []
        prons: list[int] = []
        flus: list[int] = []
        rhys: list[int] = []
        for s in active:
            pron, flu, rhy, total = _sentence_scores(s)
            sentences.append(
                SentenceScoreOut(
                    sentence=s.korean_sentence or "",
                    pronunciation=pron,
                    fluency=flu,
                    rhythm=rhy,
                )
            )
            totals.append(total)
            prons.append(pron)
            flus.append(flu)
            rhys.append(rhy)

        total_n = len(active)
        passed = sum(1 for t in totals if t >= _PASS_THRESHOLD)
        overall = round(sum(totals) / total_n) if totals else 0

        # ── 음소(목): 가장 어려웠던 소리 = 정확도 최저, evidence 는 숫자로 조립 ──
        hardest = _hardest(_MOCK_PHONEME_POOL)
        hardest_sound = hardest.sound if hardest else ""
        hardest_evidence = (
            f"{hardest.sound}에서 {hardest.attempts}번 중 "
            f"{hardest.attempts - hardest.correct}번 새어 나갔어요"
            if hardest
            else ""
        )
        l1_interference = await _generate_l1_feedback(client, native_locale, hardest)

        return PronunciationReport(
            passed=passed,
            total=total_n,
            date=call.call_date or datetime.now(timezone.utc),
            overall=overall,
            pronunciation=round(sum(prons) / len(prons)) if prons else 0,
            fluency=round(sum(flus) / len(flus)) if flus else 0,
            rhythm=round(sum(rhys) / len(rhys)) if rhys else 0,
            # ── 음소 부분: 목(모델 붙으면 풀만 실집계로 교체) ──
            hardest_sound=hardest_sound,
            hardest_evidence=hardest_evidence,
            l1_interference=l1_interference,
            phonemes=_select_phonemes(_MOCK_PHONEME_POOL),
            sentences=sentences,
            sessions=self._recent_sessions(member_id),
        )

    def _recent_sessions(self, member_id: int) -> list[SessionPointOut]:
        """회원 최근 통화 5건 집계(oldest first). 실데이터."""
        recent = (
            self.db.query(Call)
            .options(selectinload(Call.sentences).joinedload(Sentence.evaluation))
            .filter(Call.member_id == member_id)
            .order_by(Call.call_date.desc(), Call.call_id.desc())
            .limit(_RECENT_SESSIONS)
            .all()
        )
        recent = list(reversed(recent))  # oldest first (그래프 좌→우)

        today = datetime.now(timezone.utc).date()
        points: list[SessionPointOut] = []
        prev_score: int | None = None
        for call in recent:
            n, score = _call_score(call)
            d = call.call_date or datetime.now(timezone.utc)
            label = "오늘" if d.date() == today else f"{d.month}/{d.day}"
            points.append(
                SessionPointOut(
                    label=label,
                    date=f"{d.month}/{d.day}",
                    sentences=n,
                    score=score,
                    delta=None if prev_score is None else score - prev_score,
                )
            )
            prev_score = score
        return points
