"""표시 문구를 회원 모국어로 — §6 차시 상황 한 줄 · §10 통화 기록 제목(2026-09-29).

⭐ 방식(bt-back 확정): **전량 미리 번역하지 않는다. 요청된 (항목, 언어)만 번역하고 캐시한다.**
  캐시에 있으면 0콜, 없으면 core/text_translate 1콜(§10 은 한 페이지의 미스분을 **묶어 1콜**).
⛔ 언어는 요청에 싣지 않는다 — `member.language` 로 고른다(`display_locale`).
⛔ 실패는 조회를 막지 않는다(R5) — 이 모듈의 공개 함수는 예외를 내지 않는다. 번역 실패는
  캐시하지 않는다(다음 조회에 다시 시도). 캐시 적재 실패(동시 요청의 유니크 충돌 등)는 번역을
  그대로 돌려주고 캐시만 포기한다.
⛔ 원문(`cur_lesson.situation` · `call.summary`)은 절대 덮어쓰지 않는다 — situation 은 통화
  프롬프트에 들어가고, summary 는 학습자 것이다.
문서: docs/20260929_0050_차시제목-통화제목-다국어-요청시번역.md
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from core import text_translate
from core.config import settings
from core.languages import count_target_script_chars, normalize_locale
from core.prompts.common import DEFAULT_LOCALE, LOCALE_LABEL
from domains.learning.repository import display_i18n_repository as repo

logger = logging.getLogger(__name__)

KIND_CUR_SITUATION = "cur_situation"
#: cur_lesson.partner(상대역) — 회화학습 힌트 시트 「이번 대화」 상대 줄(2026-10-10 PM-DEC-485).
KIND_CUR_PARTNER = "cur_partner"


def display_locale(member_language: Optional[str]) -> str:
    """회원의 표시 언어. 정규화("en-US"→"en")하고, 없으면 en.

    ⚠ `member.language`(모국어)다 — `target_language`(배우는 언어)가 아니다.
    NULL→en 은 취약발음 `weak_sound_service._locale_of` 와 같은 폴백이다(외국인 앱이라 모르면
    한국어보다 영어). NULL 회원은 온보딩 전이라 이 화면에 거의 닿지 않는다.
    """
    return normalize_locale(member_language) or "en"


def summary_lang_for(locale: Optional[str]) -> str:
    """분석이 요약을 **실제로 쓰는** 언어. 분석 지시문이 `LOCALE_LABEL.get(locale, 영어)` 라
    표 밖 locale(ne·my 등)은 영어로 쓰인다 — 그 규칙을 그대로 따라 저장해야 목록이 «이미 회원
    언어다»를 맞게 판정한다."""
    code = normalize_locale(locale)
    return code if code in LOCALE_LABEL else DEFAULT_LOCALE


# --------------------------------------------------------------------------- #
# §6 — GET /cur/me lesson.situation_translation
# --------------------------------------------------------------------------- #
def situation_translation(
    db: Session, client, situation: Optional[str], locale: str,
) -> Optional[str]:
    """차시 상황 한 줄의 회원 언어 번역. 없거나 못 만들면 None(앱은 한국어만 표시).

    locale 이 ko 면 None — 원문이 한국어다(ko 행은 만들지 않는다).
    """
    return _cur_text_translation(db, client, KIND_CUR_SITUATION, situation, locale)


def partner_translation(
    db: Session, client, partner: Optional[str], locale: str,
) -> Optional[str]:
    """차시 상대역(cur_lesson.partner)의 회원 언어 번역 — situation_translation 과 같은 캐시·같은 번역기.

    kind 만 cur_partner 로 갈라 같은 한국어 문구가 상황·상대역 양쪽에 나와도 행이 섞이지 않는다.
    """
    return _cur_text_translation(db, client, KIND_CUR_PARTNER, partner, locale)


def _cur_text_translation(
    db: Session, client, kind: str, source: Optional[str], locale: str,
) -> Optional[str]:
    """cur_* 한국어 표시 문구 하나의 회원 언어 번역 — 캐시(cur_text_i18n) 우선, 없으면 번역해 저장. 실패는 None."""
    src = (source or "").strip()
    if not src or locale == "ko":
        return None
    try:
        cached = repo.get_cur_text(db, kind, src, locale)
        if cached:
            return cached
        got = text_translate.translate_texts(
            client, settings.TRANSLATE_MODEL, [src], locale,
            timeout_s=settings.TRANSLATE_TIMEOUT_S,
        )
        text = got[0] if got else None
        if not text:
            return None
        _save(db, lambda: repo.add_cur_text(db, kind, src, locale, text),
              what=f"cur_text_i18n kind={kind} locale={locale}")
        return text
    except Exception:  # noqa: BLE001 - 번역·캐시 실패가 /cur/me 를 막으면 안 된다(R5)
        logger.warning("display_i18n: %s 번역 실패(null) locale=%s", kind, locale, exc_info=True)
        _rollback(db)
        return None


# --------------------------------------------------------------------------- #
# §10 — GET /calls summary
# --------------------------------------------------------------------------- #
def _source_lang(summary: str, summary_lang: Optional[str]) -> Optional[str]:
    """요약 원문 언어. 저장값이 있으면 그것, 없으면(과거 행) 한글이 있으면 ko, 아니면 모름."""
    if summary_lang:
        return summary_lang
    return "ko" if count_target_script_chars(summary, "ko") > 0 else None


def localized_summaries(db: Session, client, calls: Iterable, locale: str) -> dict[int, str]:
    """{call_id: 회원 언어 요약} — 번역이 **필요하고 얻은** 것만 담는다(없는 call 은 원문 그대로).

    calls 는 **목록 한 페이지**(GET /calls 의 limit)다 — 전체 이력을 돌리지 않는다.
    원문 언어를 모르는 과거 행(한글 없음)은 대상이 ko 가 아니어도 번역에 넣는다 — 비-ko 끼리
    (en vs de)는 구분할 수 없어서다. 번역기가 «이미 그 언어면 그대로» 돌려준다.
    """
    try:
        need: list[tuple[int, str, Optional[str]]] = []
        for c in calls:
            s = (getattr(c, "summary", None) or "").strip()
            if not s:
                continue
            src = _source_lang(s, getattr(c, "summary_lang", None))
            if src == locale:
                continue
            need.append((c.call_id, s, src))
        if not need:
            return {}
        out = repo.get_summary_translations(db, [cid for cid, _, _ in need], locale)
        miss = [n for n in need if n[0] not in out]
        if miss:
            got = text_translate.translate_texts(
                client, settings.TRANSLATE_MODEL, [s for _, s, _ in miss], locale,
                timeout_s=settings.TRANSLATE_TIMEOUT_S,
            )
            if got:
                rows = [(cid, t, src) for (cid, _, src), t in zip(miss, got) if t]
                out.update({cid: t for cid, t, _ in rows})
                _save(db, lambda: repo.add_summary_translations(db, rows, locale),
                      what=f"call_summary_translation locale={locale} n={len(rows)}")
        return out
    except Exception:  # noqa: BLE001 - 번역·캐시 실패가 통화 목록을 막으면 안 된다(R5)
        logger.warning("display_i18n: 요약 번역 실패(원문) locale=%s", locale, exc_info=True)
        _rollback(db)
        return {}


# --------------------------------------------------------------------------- #
def _save(db: Session, add, *, what: str) -> None:
    """캐시 적재 + commit. 실패(동시 요청의 유니크 충돌 등)는 캐시만 포기한다."""
    try:
        add()
        db.commit()
    except Exception as exc:  # noqa: BLE001 - 캐시 실패는 응답과 무관
        logger.info("display_i18n: 캐시 적재 건너뜀(%s): %s", what, exc)
        _rollback(db)


def _rollback(db: Session) -> None:
    try:
        db.rollback()
    except Exception:  # noqa: BLE001
        pass
