"""call 라우터 — 통화 저장/목록/상세/원본/평점/삭제."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from core.deps import CurrentMember, DbSession, PageParams
from domains.learning.realtime.call_session import trigger_reanalysis
from domains.learning.schemas.call import (
    CallCreate,
    CallDetail,
    CallRatingUpdate,
    CallResult,
    CallSummary,
    RawDataOut,
)
from domains.learning.service import normalcall_service as svc
from domains.learning.service.call_service import CallService

router = APIRouter(prefix="/calls", tags=["calls"])


@router.post("", response_model=CallDetail, status_code=status.HTTP_201_CREATED)
def create_call(data: CallCreate, member: CurrentMember, db: DbSession) -> CallDetail:
    """통화 기록 생성 — 캐릭터·시작 정보를 저장한다."""
    return CallService(db).create_call(member.member_id, data)


@router.get("", response_model=list[CallSummary])
def list_calls(
    member: CurrentMember, db: DbSession, page: PageParams = Depends()
) -> list[CallSummary]:
    """내 통화 목록(최신순) — 요약·평점·상태, 페이지네이션."""
    return CallService(db).list_calls(member.member_id, page.limit, page.offset)


@router.get("/{call_id}", response_model=CallDetail)
def get_call(call_id: int, member: CurrentMember, db: DbSession) -> CallDetail:
    """통화 상세(내 통화만, 타인/없는 통화면 404)."""
    return CallService(db).get_call(member.member_id, call_id)


@router.get("/{call_id}/result", response_model=CallResult)
def get_call_result(call_id: int, member: CurrentMember, db: DbSession) -> CallResult:
    """통화 종료 후 결과 화면 — 평가 평균 + 문장 전체."""
    return CallService(db).get_call_result(member.member_id, call_id)


@router.get("/{call_id}/raw", response_model=list[RawDataOut])
def get_call_raw(call_id: int, member: CurrentMember, db: DbSession) -> list[RawDataOut]:
    """통화 원본 대화 — 턴별 화자·전사·음성 URL(순서대로)."""
    return CallService(db).get_raw(member.member_id, call_id)


@router.patch("/{call_id}", response_model=CallSummary)
def update_rating(
    call_id: int, data: CallRatingUpdate, member: CurrentMember, db: DbSession
) -> CallSummary:
    """통화 만족도(평점 1~3) 수정."""
    return CallService(db).update_rating(member.member_id, call_id, data.rating)


@router.delete("/{call_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_call(call_id: int, member: CurrentMember, db: DbSession) -> None:
    """통화 삭제 — 연관 문장·원본·평가가 CASCADE 로 함께 삭제된다."""
    CallService(db).delete_call(member.member_id, call_id)


@router.post("/{call_id}/reanalyze")
async def reanalyze_call(call_id: int, request: Request, member: CurrentMember) -> dict:
    """실패(status='failed')한 통화의 통화후 분석을 다시 돌린다(수동 재시도).

    전사(call_raw_data)는 실패해도 보존되므로 재료로 재분석하며, 증거 재적립 멱등 가드가
    중복을 막는다. status 를 'analyzing' 으로 되돌리고 백그라운드 분석을 띄운 뒤 즉시 반환
    (프론트는 기존 /status 폴링으로 done 을 기다린다). 'failed' 가 아니면 409, 타인/없는
    통화면 404, 분석 스택 미준비면 503.

    ⚠️ async 엔드포인트 — asyncio.create_task(백그라운드 분석)는 이벤트루프가 필요하다.
    DB 는 svc.run_db(threadpool)로 오프로드해 루프를 막지 않는다.
    """
    client = getattr(request.app.state, "genai_client", None)
    settings = getattr(request.app.state, "settings", None)
    session_factory = getattr(request.app.state, "session_factory", None)
    if client is None or settings is None or session_factory is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "분석 서비스가 준비되지 않았습니다."
        )
    result = await svc.run_db(
        session_factory, lambda db: svc.prepare_reanalysis(db, call_id, member.member_id)
    )
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다.")
    if not result["eligible"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"재분석 대상이 아닙니다(현재 상태: {result['status']}). "
            "'failed' 통화만 재분석할 수 있습니다.",
        )
    trigger_reanalysis(
        settings, client, session_factory, result["locale"],
        call_id=call_id, call_type=result["call_type"], member_id=member.member_id,
    )
    return {"call_id": call_id, "status": "analyzing"}
