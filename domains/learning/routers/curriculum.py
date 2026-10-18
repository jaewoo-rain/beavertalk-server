"""cur 라우터 — 커리큘럼 2단계 조회(GET /cur/me · GET /cur/lessons). 쓰기는 통화(WS)와 dev 도구(/__dev/cur-reset)만 한다.

경로는 learning 라우터 아래라 최종 URL 은 `/api/v1/cur/me` · `/api/v1/cur/lessons`(main.py API_PREFIX). 라우터는 얇다 —
인증 + service 호출 + DTO 검증. 회원의 `language` 는 **모국어**(뜻 로케일)이고 커리큘럼 언어는 회원의 `target_language` 를
**통화와 같은 해석기**(core.languages.resolve_target_language, 미지원·없음 → DEFAULT_TARGET_LANGUAGE)로 푼 코드다(2026-09-13 ja 배선).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Path, Query, status

from core.config import settings
from core.deps import CurrentMember, DbSession, GenaiClient
from core.languages import resolve_target_language
from domains.learning.schemas.curriculum import CurItemTtsOut, CurLessonItemsOut, CurLessonRowOut, CurMeOut
from domains.learning.service import cur_item_tts_service as tts_svc
from domains.learning.service import curriculum_service as cur_svc
from domains.learning.service import display_i18n_service as i18n_svc

router = APIRouter(prefix="/cur", tags=["curriculum"])


@router.get("/me", response_model=CurMeOut)
def get_me(member: CurrentMember, db: DbSession, client: GenaiClient) -> CurMeOut:
    """내 현재 차시·상태·진행률·열 수 있는 코스. 첫 호출이면 포인터(1차시)를 만든다(멱등). 언어 = 회원 target_language.

    §6(2026-09-29): lesson.situation_translation = situation 의 회원 **모국어**(member.language) 번역 — 요청에 언어를
    싣지 않는다. 캐시에 없으면 이 요청에서 번역해 저장하고, 실패하면 null(조회는 그대로 200).
    lesson.partner_translation(2026-10-10 PM-DEC-485)도 같은 경로·같은 규칙이다(kind 만 cur_partner).
    """
    out = cur_svc.me(db, member.member_id, _language_of(member))
    if out.get("lesson"):
        locale = i18n_svc.display_locale(member.language)
        out["lesson"]["situation_translation"] = i18n_svc.situation_translation(
            db, client, out["lesson"].get("situation"), locale,
        )
        out["lesson"]["partner_translation"] = i18n_svc.partner_translation(
            db, client, out["lesson"].get("partner"), locale,
        )
        # R1(2026-10-16 PM-DEC-673) — 주제 번역 · 없으면 원문(새 필드라 null 로 비우지 않는다)
        out["lesson"]["topic_translation"] = i18n_svc.topic_translation(
            db, client, out["lesson"].get("topic"), locale,
        )
        # PM-DEC-785 — 학습자 역할 번역 · partner_translation 과 같은 규칙(없으면 null)
        out["lesson"]["learner_role_translation"] = i18n_svc.learner_role_translation(
            db, client, out["lesson"].get("learner_role"), locale,
        )
    return CurMeOut.model_validate(out)


@router.get("/lessons", response_model=list[CurLessonRowOut])
def list_lessons(
    member: CurrentMember,
    db: DbSession,
    level: Optional[int] = Query(None, ge=1, description="레벨 번호로 필터(없으면 전부)"),
) -> list[CurLessonRowOut]:
    """차시 목록(no 순) + 내 상태. 안 시작한 차시는 status null, 현재 포인터 차시는 learning. 언어 = 회원 target_language.

    R2(2026-10-16 PM-DEC-673): 행마다 situation_translation — 캐시만 보고(번역 호출 0), 없으면 원문.
    """
    rows = cur_svc.lessons(db, member.member_id, level, _language_of(member))
    tr = i18n_svc.cached_situation_translations(
        db, (r.get("situation") for r in rows), i18n_svc.display_locale(member.language),
    )
    for r in rows:
        r["situation_translation"] = tr.get((r.get("situation") or "").strip())
    return [CurLessonRowOut.model_validate(r) for r in rows]


@router.get("/lessons/{no}/items", response_model=CurLessonItemsOut)
def lesson_items(
    member: CurrentMember,
    db: DbSession,
    no: int = Path(..., ge=1, description="차시 번호(CurLessonOut.no)"),
) -> CurLessonItemsOut:
    """차시 표현 목록(PM-DEC-780) — 통화 i 시트 「이번 차시 표현」. 언어 = 회원 target_language · 뜻 = 회원 모국어(없으면 en).

    chunk·must·support·core 만 seq 순으로 준다(grammar 제외 · 은퇴 제외). 예문·회원 진도는 싣지 않는다. 없는 차시는 404.
    """
    out = cur_svc.lesson_items_view(db, no, _language_of(member), i18n_svc.display_locale(member.language))
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "차시를 찾을 수 없습니다.")
    return CurLessonItemsOut.model_validate(out)


@router.post("/items/{item_id}/tts", response_model=CurItemTtsOut)
async def item_tts(
    member: CurrentMember,
    db: DbSession,
    item_id: int = Path(..., ge=1, description="cur_item.item_id"),
) -> CurItemTtsOut:
    """차시 표현 항목 소리(PM-DEC-796) — 같은 항목은 한 번만 합성해 GCS 에 둔다 · 서명 URL 을 돌려준다.

    로그인 회원만 · 없는·은퇴 항목은 404 · 합성·저장 실패는 503(앱은 버튼을 비활성으로).
    """
    return CurItemTtsOut.model_validate(await tts_svc.item_tts(db, item_id))


def _language_of(member) -> str:
    """회원의 학습 대상 언어 코드 — 통화(call_session._resolve_target_language)와 같은 해석기·같은 폴백."""
    return resolve_target_language(getattr(member, "target_language", None), default_code=settings.DEFAULT_TARGET_LANGUAGE).code
