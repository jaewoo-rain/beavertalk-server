"""call 라우터 — 통화 저장/목록/상세/원본/평점/삭제."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from core.deps import CurrentMember, DbSession, PageParams
from domains.learning.schemas.call import (
    CallCreate,
    CallDetail,
    CallRatingUpdate,
    CallResult,
    CallSummary,
    RawDataOut,
)
from domains.learning.service.call_service import CallService

router = APIRouter(prefix="/calls", tags=["calls"])


@router.post("", response_model=CallDetail, status_code=status.HTTP_201_CREATED)
def create_call(data: CallCreate, member: CurrentMember, db: DbSession) -> CallDetail:
    return CallService(db).create_call(member.member_id, data)


@router.get("", response_model=list[CallSummary])
def list_calls(
    member: CurrentMember, db: DbSession, page: PageParams = Depends()
) -> list[CallSummary]:
    return CallService(db).list_calls(member.member_id, page.limit, page.offset)


@router.get("/{call_id}", response_model=CallDetail)
def get_call(call_id: int, member: CurrentMember, db: DbSession) -> CallDetail:
    return CallService(db).get_call(member.member_id, call_id)


@router.get("/{call_id}/result", response_model=CallResult)
def get_call_result(call_id: int, member: CurrentMember, db: DbSession) -> CallResult:
    """통화 종료 후 결과 화면 — 평가 평균 + 문장 전체."""
    return CallService(db).get_call_result(member.member_id, call_id)


@router.get("/{call_id}/raw", response_model=list[RawDataOut])
def get_call_raw(call_id: int, member: CurrentMember, db: DbSession) -> list[RawDataOut]:
    return CallService(db).get_raw(member.member_id, call_id)


@router.patch("/{call_id}", response_model=CallSummary)
def update_rating(
    call_id: int, data: CallRatingUpdate, member: CurrentMember, db: DbSession
) -> CallSummary:
    return CallService(db).update_rating(member.member_id, call_id, data.rating)


@router.delete("/{call_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_call(call_id: int, member: CurrentMember, db: DbSession) -> None:
    CallService(db).delete_call(member.member_id, call_id)
