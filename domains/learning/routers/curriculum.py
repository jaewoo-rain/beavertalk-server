"""cur 라우터 — 커리큘럼 2단계 조회(GET /cur/me · GET /cur/lessons). 쓰기는 통화(WS)와 dev 도구(/__dev/cur-reset)만 한다.

경로는 learning 라우터 아래라 최종 URL 은 `/api/v1/cur/me` · `/api/v1/cur/lessons`(main.py API_PREFIX). 라우터는 얇다 —
인증 + service 호출 + DTO 검증. 회원의 `language` 는 **모국어**(뜻 로케일)이고 커리큘럼 언어는 회원의 `target_language` 를
**통화와 같은 해석기**(core.languages.resolve_target_language, 미지원·없음 → DEFAULT_TARGET_LANGUAGE)로 푼 코드다(2026-09-13 ja 배선).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from core.config import settings
from core.deps import CurrentMember, DbSession, GenaiClient
from core.languages import resolve_target_language
from domains.learning.schemas.curriculum import CurLessonRowOut, CurMeOut
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
    return CurMeOut.model_validate(out)


@router.get("/lessons", response_model=list[CurLessonRowOut])
def list_lessons(
    member: CurrentMember,
    db: DbSession,
    level: Optional[int] = Query(None, ge=1, description="레벨 번호로 필터(없으면 전부)"),
) -> list[CurLessonRowOut]:
    """차시 목록(no 순) + 내 상태. 안 시작한 차시는 status null, 현재 포인터 차시는 learning. 언어 = 회원 target_language."""
    return [CurLessonRowOut.model_validate(r) for r in cur_svc.lessons(db, member.member_id, level, _language_of(member))]


def _language_of(member) -> str:
    """회원의 학습 대상 언어 코드 — 통화(call_session._resolve_target_language)와 같은 해석기·같은 폴백."""
    return resolve_target_language(getattr(member, "target_language", None), default_code=settings.DEFAULT_TARGET_LANGUAGE).code
