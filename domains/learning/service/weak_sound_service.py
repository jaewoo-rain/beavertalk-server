"""취약 발음 학습 서비스 — 목록 조립 · 과 조회 · 평가 점수 반영.

레이어 규율: 라우터 → 이 서비스 → weak_sound_repository → models. 쓰기(점수 upsert)는
이 서비스가 `db.commit()`(R3 명시적 커밋).

점수는 두 출처를 겹쳐 쓴다.
- **학습한 소리**: `member_sound_score.score`(평가 단계 제출값).
- **안 한 소리**: `aggregate_sounds()` 가 낸 복습 집계 `pronunciation_avg`.
학습이 집계를 덮는다. 집계는 통화에서 저절로 나온 값이고, 학습 점수는 그 소리를 겨눠서
낸 값이라 더 최신·더 정확하다. 결과 화면의 「학습 전」 막대가 집계값(baseline)이다.

국적별 목록은 `speak_country.first_country`(영문 국가명)를 **ISO 2자리로 접어서** 찾는다
(`core.nationality.iso_for_country`). 이름끼리 맞추면 표기가 갈린 나라가 조용히 빈 목록이
된다 — 실측(2026-09-21)으로 모델은 `Russia`, 통계는 `Russian Federation` 이었다.
통계가 없는 나라(미수록 7개국·Korea 포함)면 **국적 섹션만 빈 채로** 내려간다 — 화면 전체를
실패시키지 않는다(R5 정신).
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from core.nationality import iso_for_country
from core.speechsuper import assess_pronunciation
from domains.learning.models.member_sound_score import MemberSoundScore
from domains.learning.models.sound_lesson import SoundLesson
from domains.learning.repository.pronunciation_repository import PronunciationRepository
from domains.learning.repository.weak_sound_repository import WeakSoundRepository
from domains.learning.schemas.weak_sound import (
    NationalWeakSounds,
    SoundLessonOut,
    SoundResultOut,
    WeakSoundItem,
    WeakSoundListOut,
)
from domains.learning.service.pronunciation_service import aggregate_sounds

logger = logging.getLogger(__name__)

# 목록에 노출할 개수. 국적별·내 취약 각각 상위 N개(2026-09-20 사용자 확정 = 5).
LIST_SIZE = 5
# 추천 기준선 — 이 점수 미만이면 「먼저 해보세요」 후보다.
RECOMMEND_BELOW = 80
# 내 취약 발음 집계에 쓰는 최근 통화 수. 통화 1건만 보면 그날 컨디션이 목록을 흔든다.
RECENT_CALLS = 5


def _score_view(
    lesson: SoundLesson,
    row: Optional[MemberSoundScore],
    agg: dict[str, float],
    share: Optional[int] = None,
) -> WeakSoundItem:
    """학습 점수 우선, 없으면 복습 집계 → 카드 1장."""
    if row is not None:
        score: Optional[int] = row.score
        learned = True
        baseline = row.baseline_score
        attempts = row.attempts
        last = row.last_learned_at
    else:
        avg = agg.get(lesson.sound_key)
        score = int(round(avg)) if avg is not None else None
        learned = False
        baseline = None
        attempts = 0
        last = None
    return WeakSoundItem(
        sound_key=lesson.sound_key,
        label=lesson.label,
        card_desc=lesson.card_desc,
        type=lesson.type,
        diagram=lesson.diagram,
        score=score,
        learned=learned,
        baseline_score=baseline,
        attempts=attempts,
        share=share,
        last_learned_at=last,
    )


def _aggregated(db: Session, member_id: int) -> dict[str, float]:
    """최근 통화의 복습 집계 → {sound_key: pronunciation_avg}.

    `sound_key` 가 없는(위치 미부착) 옛 집계는 버린다 — 학습 단위와 맞출 수 없다.
    """
    reviews = WeakSoundRepository(db).recent_counted_reviews(member_id, RECENT_CALLS)
    return {
        s.sound_key: s.pronunciation_avg
        for s in aggregate_sounds(reviews)
        if s.sound_key is not None
    }


def get_weak_sounds(db: Session, member_id: int) -> WeakSoundListOut:
    """목록 화면 — 국적별 N개 + 내 취약 N개 + 추천 1개."""
    repo = WeakSoundRepository(db)
    lessons = {l.sound_key: l for l in repo.get_lessons()}
    scores = repo.get_scores(member_id)
    agg = _aggregated(db, member_id)

    country = PronunciationRepository(db).get_first_country(member_id)
    country_iso = iso_for_country(country)
    if country and country_iso is None:
        # 분류기가 낸 이름이 라벨표에 없다. 그 나라 사용자는 국적 섹션을 못 본다 —
        # 조용히 넘기면 아무도 모르므로 로그로 남긴다.
        logger.warning("국적 라벨을 ISO 로 못 바꿨다: %s", country)
    national_items: list[WeakSoundItem] = []
    if country_iso:
        for stat in repo.get_national_stats(country_iso):
            lesson = lessons.get(stat.sound_key)
            if lesson is None:
                # 시드 불일치. 조용히 넘기되 로그는 남긴다 — 앱에 빈 카드를 띄우지 않는다.
                logger.warning("national_sound_stat 이 없는 과를 가리킨다: %s", stat.sound_key)
                continue
            national_items.append(
                _score_view(lesson, scores.get(stat.sound_key), agg, share=stat.share)
            )
            if len(national_items) >= LIST_SIZE:
                break

    # 내 취약 발음 — 점수 낮은 순. 표본이 없는 소리는 애초에 목록에 없다(agg 키 기준).
    mine_keys = sorted(agg, key=lambda k: (agg[k], k))
    mine_items: list[WeakSoundItem] = []
    national_keys = {i.sound_key for i in national_items}
    for key in mine_keys:
        lesson = lessons.get(key)
        if lesson is None or key in national_keys:
            continue  # 같은 소리를 두 목록에 중복 노출하지 않는다
        mine_items.append(_score_view(lesson, scores.get(key), agg))
        if len(mine_items) >= LIST_SIZE:
            break

    return WeakSoundListOut(
        national=NationalWeakSounds(country=country, items=national_items),
        mine=mine_items,
        recommended=_recommend(national_items, mine_items),
    )


def _recommend(
    national: list[WeakSoundItem], mine: list[WeakSoundItem]
) -> Optional[str]:
    """국적별 1순위 중 80점 미만 첫 소리. 없으면 내 취약 1순위. 그것도 없으면 None.

    점수가 None(표본 없음)인 소리는 **추천 대상이다** — 한 번도 안 해본 소리를 먼저
    권하는 편이 이미 90점인 소리를 권하는 것보다 낫다.
    """
    for item in national:
        if item.score is None or item.score < RECOMMEND_BELOW:
            return item.sound_key
    for item in mine:
        if item.score is None or item.score < RECOMMEND_BELOW:
            return item.sound_key
    return None


def get_lesson(db: Session, member_id: int, sound_key: str) -> Optional[SoundLessonOut]:
    """4단계 콘텐츠 전량 + 현재 점수. 없는 소리면 None(라우터가 404)."""
    repo = WeakSoundRepository(db)
    lesson = repo.get_lesson(sound_key)
    if lesson is None:
        return None
    row = repo.get_score(member_id, sound_key)
    if row is not None:
        score: Optional[int] = row.score
        learned = True
    else:
        avg = _aggregated(db, member_id).get(sound_key)
        score = int(round(avg)) if avg is not None else None
        learned = False
    return SoundLessonOut(
        sound_key=lesson.sound_key,
        label=lesson.label,
        type=lesson.type,
        position=lesson.position,
        jamo=lesson.jamo,
        diagram=lesson.diagram,
        card_desc=lesson.card_desc,
        payload=lesson.payload,
        score=score,
        learned=learned,
    )


def assess(
    db: Session,
    member_id: int,
    sound_key: str,
    raw: bytes,
    content_type: Optional[str] = None,
) -> Optional[SoundResultOut]:
    """평가 단계 녹음 채점 + 점수 반영 → 전/후 점수. 없는 소리면 None(라우터가 404).

    ★ **채점은 서버가 한다**(2026-09-20 사용자 확정). 클라가 계산한 점수를 받는 설계를
      폐기했다 — 앱이 100 을 보내면 그대로 들어가기 때문이다. 앱은 녹음만 올린다.

    평가 문장은 `sound_lesson.payload["test"]["text"]` 다. `sentence` 테이블에 행이 없어서
    기존 복습 경로(`POST /sentences/{id}/reviews/audio`)를 쓸 수 없다 — 그쪽은 저장된
    문장 행을 전제한다. 그래서 SpeechSuper 를 직접 부르고 복습 이력은 남기지 않는다
    (통화 문장이 아닌 것을 복습 목록에 섞지 않는다).

    점수 규칙
    - 첫 제출이면 `baseline_score` 에 **직전 집계값**을 박는다. 두 번째부터는 건드리지
      않는다 — 「학습 전」은 한 번만 일어나는 사건이다.
    - `before` 는 직전에 화면에 보이던 점수다(학습 이력이 있으면 그 점수, 없으면 집계값).
    - 재도전으로 점수가 내려가도 `best_score` 는 유지한다.
    """
    repo = WeakSoundRepository(db)
    lesson = repo.get_lesson(sound_key)
    if lesson is None:
        return None

    scored = _score_audio(lesson, raw)
    value = scored["score"]
    row = repo.get_score(member_id, sound_key)
    now = datetime.now(timezone.utc)

    if row is None:
        avg = _aggregated(db, member_id).get(sound_key)
        baseline = int(round(avg)) if avg is not None else None
        before = baseline
        row = repo.add_score(MemberSoundScore(
            member_id=member_id, sound_key=sound_key, score=value,
            best_score=value, attempts=1, baseline_score=baseline, last_learned_at=now,
        ))
    else:
        before = row.score
        row.score = value
        row.best_score = max(row.best_score, value)
        row.attempts += 1
        row.last_learned_at = now

    db.commit()
    return SoundResultOut(
        sound_key=sound_key,
        label=lesson.label,
        before=before,
        after=value,
        delta=(value - before) if before is not None else None,
        best_score=row.best_score,
        attempts=row.attempts,
        text=scored["text"],
        char_scores=scored["char_scores"],
        phoneme_misses=scored["phoneme_misses"],
    )


def _score_audio(lesson: SoundLesson, raw: bytes) -> dict:
    """평가 문장 + 녹음 → {score, text, char_scores, phoneme_misses}.

    SpeechSuper 는 파일 경로/URL 을 받으므로 임시 .wav 로 떨어뜨려 넘긴다(복습 경로와
    같은 방식). 녹음을 스토리지에 저장하지 않는다 — 이 화면은 점수만 쓰고 다시 듣기가
    없다. 나중에 다시 듣기가 생기면 그때 저장을 붙인다.

    키·오디오 문제로 벤더가 죽어도 speechsuper 가 스텁으로 폴백한다(예외 없음, R5).

    ⚠ `phoneme_misses` 는 **dev 의 speechsuper 가 아직 내지 않는다**(그 기능은
      `feat/pronunciation` 에만 있고 dev 로 병합되지 않았다). 그래서 지금은 항상 빈
      리스트다. 필드를 미리 열어 두는 이유는 그 기능이 병합되면 서버·앱 양쪽에 손댈
      필요 없이 값이 채워지기 때문이다.
    """
    text = ((lesson.payload or {}).get("test") or {}).get("text") or ""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
        f.write(raw)
        tmp_path = f.name
    try:
        result = assess_pronunciation(text, tmp_path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
    evaluation = result.get("evaluation") or {}
    score = evaluation.get("pronunciation")
    if score is None:
        score = evaluation.get("total_score") or 0
    return {
        "score": max(0, min(100, int(score))),
        "text": text,
        "char_scores": result.get("char_scores") or [],
        "phoneme_misses": result.get("phoneme_misses") or [],
    }
