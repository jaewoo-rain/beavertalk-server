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

from core import storage
from core.config import settings
from core.nationality import iso_for_country
from core.speechsuper import assess_pronunciation
from domains.learning.models.member_sound_score import MemberSoundScore
from domains.learning.models.sound_audio import (
    LESSON_ENGINE,
    LESSON_VOICE,
    audio_text_hash,
)
from domains.learning.models.sound_lesson import SoundLesson
from domains.learning.models.sound_lesson_i18n import SoundLessonI18n
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


def _locale_of(db: Session, member_id: int) -> str:
    """회원의 표시 언어. 없으면 en.

    ⚠ `member.language`(모국어)다. `target_language`(배우는 언어=한국어)가 아니다 —
    바꿔 쓰면 외국인에게 한국어 설명이 나간다.
    """
    lang = PronunciationRepository(db).get_member_language(member_id)
    return (lang or "en").strip().lower() or "en"


def _translated(
    lesson: SoundLesson, tr: Optional[SoundLessonI18n]
) -> tuple[str, str, dict]:
    """(label, card_desc, payload) 를 번역본으로 덮어쓴다. 없는 항목은 **원본 유지**.

    ⛔ 빈 번역으로 원본을 지우지 마라. 번역이 없는 언어에서 화면이 비어 버린다 —
    한국어라도 보이는 편이 아무것도 없는 것보다 낫다.

    payload 는 **깊은 병합**이 아니라 「번역되는 자리만」 갈아 끼운다. words 는 순서로
    맞춘다(원본과 번역의 길이가 다르면 짧은 쪽까지만).
    """
    if tr is None:
        return lesson.label, lesson.card_desc, lesson.payload
    label = tr.label or lesson.label
    card_desc = tr.card_desc or lesson.card_desc
    payload = dict(lesson.payload or {})
    tp = tr.payload or {}

    if tp.get("how_to"):
        payload["how_to"] = tp["how_to"]

    # ⚠ payload 는 JSON 이다. dict 가 아닌 항목은 손대지 않고 그대로 통과시킨다 —
    #   번역 때문에 원본 콘텐츠가 깨지면 안 된다.
    src_words = list(payload.get("words") or [])
    tr_words = list(tp.get("words") or [])
    if src_words and tr_words:
        merged = []
        for i, w in enumerate(src_words):
            if not isinstance(w, dict):
                merged.append(w)
                continue
            w = dict(w)
            got = tr_words[i] if i < len(tr_words) else None
            if isinstance(got, dict) and got.get("meaning"):
                w["meaning"] = got["meaning"]
            merged.append(w)
        payload["words"] = merged

    for key in ("sentence", "test"):
        src = payload.get(key)
        got = (tp.get(key) or {}).get("translation")
        if isinstance(src, dict) and got:
            src = dict(src)
            src["translation"] = got
            payload[key] = src
    return label, card_desc, payload


def _audio_urls(db: Session, payload: dict) -> dict[str, str]:
    """payload 안에서 **재생되는 문장** → 지금 서명한 재생 URL.

    미리 구워 둔 것만 나온다. 없는 문장은 빠지고, 앱은 종전대로 `POST /tts/speech` 로
    떨어진다(R5) — 죽지 않는다.

    ⛔ DB 에 든 것은 object key 다. 반드시 `playback_url` 로 지금 서명한다. 저장된 서명을
      그대로 내보내면 7일 뒤 그 과는 영구히 소리가 죽는다(2026-08-31 실사고).
    """
    # ⚠ payload 는 JSON 이다 — 모양을 **믿지 않는다**. 시드가 어긋나거나 옛 행이 남아 있어도
    #   여기서 500 을 내면 학습 화면 전체가 죽는다. 음성은 부가물이라 빠지면 빠진 대로 둔다.
    def _text_of(item: object) -> str:
        if isinstance(item, str):
            return item.strip()
        if isinstance(item, dict):
            got = item.get("text")
            return got.strip() if isinstance(got, str) else ""
        return ""

    texts: list[str] = []
    for w in payload.get("words") or []:
        t = _text_of(w)
        if t:
            texts.append(t)
    s = payload.get("sentence") if isinstance(payload.get("sentence"), dict) else {}
    for item in [*(s.get("chunks") or []), s.get("text")]:
        t = _text_of(item)
        if t:
            texts.append(t)
    test = payload.get("test") if isinstance(payload.get("test"), dict) else {}
    t = _text_of(test.get("text"))
    if t:
        texts.append(t)

    by_hash = {audio_text_hash(t): t for t in texts}
    keys = WeakSoundRepository(db).get_audio(list(by_hash), LESSON_VOICE, LESSON_ENGINE)
    out: dict[str, str] = {}
    for h, key in keys.items():
        # 만료를 **명시한다.** 인자를 비우면 `public_url` 경로로 빠지는데, 이름과 달리
        # 그것도 서명 URL 이고 TTL 만 다르다(7일). 기본값에 기대면 그 함수의 기본이
        # 바뀌는 날 이 화면이 조용히 따라 바뀐다.
        # 학습 음성은 통화 문장 TTS 와 같은 성격이라 같은 TTL 을 쓴다.
        url = storage.playback_url(
            settings.SUPABASE_BUCKET_SAMPLES, key, settings.GCS_SIGNED_URL_TTS_TTL,
        )
        if url:
            out[by_hash[h]] = url
    return out


def _score_view(
    lesson: SoundLesson,
    row: Optional[MemberSoundScore],
    agg: dict[str, float],
    share: Optional[int] = None,
    label: Optional[str] = None,
    card_desc: Optional[str] = None,
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
        label=label or lesson.label,
        card_desc=card_desc or lesson.card_desc,
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
    # 번역은 언어 한 벌을 통째로 읽는다(과 30개 × 왕복 30번을 피한다).
    tr = repo.get_i18n(_locale_of(db, member_id))

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
            lb, cd, _ = _translated(lesson, tr.get(lesson.sound_key))
            national_items.append(
                _score_view(
                    lesson, scores.get(stat.sound_key), agg,
                    share=stat.share, label=lb, card_desc=cd,
                )
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
        lb, cd, _ = _translated(lesson, tr.get(key))
        mine_items.append(
            _score_view(lesson, scores.get(key), agg, label=lb, card_desc=cd)
        )
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
    tr = repo.get_i18n(_locale_of(db, member_id)).get(sound_key)
    label, card_desc, payload = _translated(lesson, tr)
    return SoundLessonOut(
        sound_key=lesson.sound_key,
        label=label,
        type=lesson.type,
        position=lesson.position,
        jamo=lesson.jamo,
        diagram=lesson.diagram,
        card_desc=card_desc,
        payload=payload,
        audio=_audio_urls(db, payload),
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
