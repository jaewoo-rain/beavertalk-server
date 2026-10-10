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
from typing import Any, Sequence

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, sessionmaker

from domains.learning.models.call import Call
from domains.learning.schemas.pronunciation import (
    PronHistoryItem,
    PronunciationReport,
    SoundAggregate,
)
from domains.learning.schemas.pronunciation_report import (
    LearningSummaryOut,
    PhonemeStatOut,
    RetrySoundOut,
    SentenceScoreOut,
    SessionPointOut,
)
from domains.learning.service import pronunciation_service as pron_svc
from domains.learning.service import weak_sound_service as weak_svc
from domains.learning.service.normalcall_service import run_db

_PASS_THRESHOLD = 80  # 문장 통과 기준(total_score ≥ 80)

# ── 「다시 해볼 소리」 기준값 (PM-DEC-333/337/341, 2026-10-03) ──────────────── #
# ⭐ 설정값(`core/config.Settings`)이 아니라 **서비스 모듈 상수**다 — 배포 환경마다
#   달라야 할 값이 아니고(화면 규칙이라 dev/prod 가 갈리면 QA 가 재현을 못 한다),
#   비밀도 아니며, 시험이 이 숫자를 고정한다(env 로 흔들리면 시험이 환경 의존이 된다).
#   같은 파일의 `_PASS_THRESHOLD`, `weak_sound_service` 의 `LIST_SIZE`·`RECENT_CALLS`
#   와 같은 자리다.
RETRY_MIN_MISSES = 2  # 이 통화에서 이만큼 틀린 소리만 카드로 띄운다
RETRY_LIMIT = 3       # 카드 최대 개수


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
    """최근 세션(oldest first) — 라벨(오늘/M/D)·날짜(M/D)·delta 조립.

    ⭐ Q8(2026-09-24) — call_date·call_id 를 이력 행 원본 그대로 싣는다.
    ⚠ label·date 는 **구버전 앱 호환**을 위해 그대로 둔다(`or datetime.now()` 폴백
      포함, 손대지 않았다) — 그 폴백은 이미 나온 문자열의 모양을 지키기 위함이고,
      새 필드 call_date 는 그 폴백을 타지 않고 h.call_date 원본을 그대로 싣는다
      («없음»을 «지금»으로 위조하지 않는다). `Call.call_date` 는 스키마상 nullable
      이지만 실제로 Call 을 만드는 두 경로(normalcall_service.py:1377,
      call_service.py:552)가 항상 채운다 — 그래서 여기서 None 이면 조용히 감추지
      않고 그대로 필수필드 검증에 맡긴다(발생하면 502 아니라 500 으로 시끄럽게
      실패해야 그게 진짜 이상 데이터라는 신호다).

    ⛔⛔ Q9(2026-09-24, bt-back 운영 실측 member_id=88) — 「점수 없음」(counted 복습이
      있는 문장이 없어 `h.score is None` — 발음 챌린지를 안 누른 통화)과 「0점」을
      더 이상 같은 값으로 뭉개지 않는다. 예전엔 `score=0` 으로 내보내 96점 다음에
      점수 없는 통화가 오면 `delta=-96`(없는 하락)이 찍혔다. 이제 점수 없는 세션은
      `score=None` 으로 그대로 내보내고(진행규칙 5 의 "키 생략" 대상이 **아니다** —
      이건 "값이 없다"는 사실 자체를 앱에 알려야 하는 필드라 키는 남긴다), `delta`
      는 **양쪽 다 점수가 있을 때만** 계산한다. `prev`(직전 비교 기준)는 점수 없는
      세션을 만나도 **안 덮는다** — 그 통화 하나가 "직전 점수 있는 세션"과의 비교
      사슬을 끊으면 안 된다(96 → 없음 → 없음 → 다음 점수 있는 통화까지도 96 대비
      delta 를 낸다).
      앱 호환 확인(`beavertalk-flutter` origin/dev `learning_summary.dart:16`):
      `_asInt(null)` 이 0 을 돌려줘 구버전은 크래시 없이 지금과 같게 보이고,
      `delta` 는 이미 null 을 "—"(no previous session)로 처리하고 있어 신버전만
      더 정확해진다.
    """
    items = list(reversed(history))  # get_pronunciation_history 는 최신순 → 오래된순으로
    today = datetime.now(timezone.utc).date()
    out: list[SessionPointOut] = []
    prev: int | None = None
    for h in items:
        d = h.call_date or datetime.now(timezone.utc)
        score = round(h.score) if h.score is not None else None
        out.append(
            SessionPointOut(
                label="오늘" if d.date() == today else f"{d.month}/{d.day}",
                date=f"{d.month}/{d.day}",
                sentences=h.sentence_count,
                score=score,
                delta=None if (prev is None or score is None) else score - prev,
                call_date=h.call_date,
                call_id=h.call_id,
            )
        )
        if score is not None:
            prev = score
    return out


def _call_date(db: Session, call_id: int) -> datetime | None:
    call = db.get(Call, call_id)
    return call.call_date if call is not None else None


# --------------------------------------------------------------------------- #
# 「다시 해볼 소리」 (PM-DEC-333/337/341, 2026-10-03 앱 요청)
# --------------------------------------------------------------------------- #
def _misses(s: SoundAggregate) -> int:
    """이 통화에서 그 소리를 틀린 횟수. 틀림 = 음소 점수 80 미만(aggregate_sounds)."""
    return s.attempts - s.passes


def _retry_candidates(sounds: Sequence[SoundAggregate]) -> list[SoundAggregate]:
    """선정 규칙 2·4·5 — DB 를 보지 않는 순수 함수.

    입력은 **이 통화의** 소리 집계(`report.sounds`)다 — 규칙 1(문장별 마지막 counted
    복습)·규칙 7(스텁은 `counted=False` 라 애초에 없다)은 그 입력이 이미 충족한다.

    - 규칙 2: `sound_key` 가 None 인 버킷 제외(모음·위치 미부착 옛 복습 — 학습 단위와
      맞출 수 없다).
    - 규칙 4: `misses >= RETRY_MIN_MISSES`.
    - 규칙 5: misses 내림차순 → 정확도(passes/attempts) 오름차순 → sound_key.
      ⚠ 정확도는 `_accuracy`(반올림 정수)가 아니라 **실수 비율**로 비교한다 — 3단
      정렬의 2단계가 동률 깨기용이라 반올림하면 일부러 넣은 변별이 뭉개진다.
    - 규칙 3(과가 있는 소리만)·규칙 6(최대 3개)은 `_retry_cards` 가 한다 — 과 조회는
      DB 가 필요하고, **자르기(6)는 과 필터(3) 뒤**여야 한다(먼저 3개로 자르면 과가
      있는 4순위가 과 없는 상위 때문에 억울하게 빠진다).
    """
    cands = [
        s for s in sounds if s.sound_key and _misses(s) >= RETRY_MIN_MISSES
    ]
    cands.sort(
        key=lambda s: (
            -_misses(s),
            (s.passes / s.attempts) if s.attempts else 0.0,
            s.sound_key or "",
        )
    )
    return cands


def _retry_cards(
    db: Session, member_id: int, candidates: Sequence[SoundAggregate]
) -> list[RetrySoundOut]:
    """선정 규칙 3·6 + 카드 조립(라벨·설명·점수).

    점수·라벨은 `weak_sound_service.get_sound_cards` 가 취약 발음 목록과 **같은 코드**로
    내준다(`_score_view`) — 두 화면의 같은 소리가 다른 점수를 보일 수 없다.
    `attempts`·`misses` 는 **이 통화** 집계라 거기서 오지 않는다(뜻이 다른 값이다 —
    `RetrySoundOut` docstring).

    과가 없는 소리는 `cards` 에 없으므로 건너뛴다(규칙 3). 그 뒤 앞에서부터
    `RETRY_LIMIT` 개를 취한다(규칙 6) — 정렬은 `_retry_candidates` 가 이미 해 뒀다.
    """
    if not candidates:
        return []
    cards = weak_svc.get_sound_cards(
        db, member_id, [s.sound_key for s in candidates if s.sound_key]
    )
    out: list[RetrySoundOut] = []
    for s in candidates:
        card = cards.get(s.sound_key or "")
        if card is None:
            continue  # 규칙 3 — 학습 과가 없는 소리(모음 등)는 띄우지 않는다
        out.append(
            RetrySoundOut(
                sound_key=card.sound_key,
                label=card.label,
                card_desc=card.card_desc,
                attempts=s.attempts,
                misses=_misses(s),
                score=card.score,
            )
        )
        if len(out) >= RETRY_LIMIT:
            break  # 규칙 6
    return out


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
    # ⭐ PM-DEC-333/337/341 — 「다시 해볼 소리」는 **새 세션을 열지 않는다.** 후보 선정은
    #   순수 함수(DB 0)이고, 카드 조회는 이미 있던 `_call_date` 의 세션에 얹는다
    #   (한 세션 = 한 커넥션 = threadpool 홉 1번 그대로). 후보가 0개면 `_retry_cards`
    #   가 즉시 빈 리스트라 추가 쿼리도 0이다.
    candidates = _retry_candidates(report.sounds)
    call_date, retry_sounds = await run_db(
        session_factory,
        lambda db: (_call_date(db, call_id), _retry_cards(db, member_id, candidates)),
    )

    # ── 문장별 + 통과·평균(실데이터) ──
    # ⛔⛔ §2 정정(2026-09-27, 앱 요청) — "미복습 점수는 0"이었던 옛 설계를 뒤집었다.
    #   미복습은 0점이 아니라 "채점을 못 했다"는 별개의 사실이라 null 로 보낸다
    #   (Q9 발음 리포트 score: int|None 과 같은 규율 — core/speechsuper.py·
    #   review_service.py·PronScoreOut·SoundResultOut 과 같은 계약으로 맞춘다).
    #   ⚠ 앱(LearningSummary 화면)의 선반영 여부는 이 커밋 시점에 미확인이다 —
    #   `_asInt(null)→0` 이라 크래시는 없지만(bt-back 확인), null 을 "-%" 로 그리려면
    #   앱 쪽 수정이 별도로 필요하다.
    def _score(value: int | None) -> int | None:
        return value if value is not None and 0 <= value <= 100 else None

    sentences: list[SentenceScoreOut] = []
    seen_ids: set[int] = set()
    for s in report.sentences:
        if not (s.korean_sentence or "").strip() or s.sentence_id in seen_ids:
            continue
        seen_ids.add(s.sentence_id)
        sentences.append(
            SentenceScoreOut(
                sentence_id=s.sentence_id,
                sentence=s.korean_sentence,
                total_score=_score(s.total_score),
                pronunciation=_score(s.pronunciation),
                fluency=_score(s.fluency),
                rhythm=_score(s.rhythm),
                kind=s.kind,
            )
        )
    totals = [s.total_score for s in sentences if s.total_score is not None]
    prons = [s.pronunciation for s in sentences if s.pronunciation is not None]
    flus = [s.fluency for s in sentences if s.fluency is not None]
    rhys = [s.rhythm for s in sentences if s.rhythm is not None]
    total_n = len(sentences)
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
        retry_sounds=retry_sounds,
    )
