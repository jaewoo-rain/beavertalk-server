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
from domains.learning.schemas.pronunciation import (
    PronHistoryItem,
    PronunciationReport,
)
from domains.learning.schemas.pronunciation_report import LearningSummaryOut
from domains.learning.service import normalcall_service as svc
from domains.learning.service import pronunciation_report_service as report_svc
from domains.learning.service import pronunciation_service as pron_svc
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


@router.get("/pronunciation-history", response_model=list[PronHistoryItem])
def get_pronunciation_history(
    member: CurrentMember, db: DbSession
) -> list[PronHistoryItem]:
    """최근5 통화(normal·done)의 발음 추이 — [날짜, 활성 문장수, counted 문장 평균 점수].

    정적 경로라 `/{call_id}` 보다 먼저 선언한다(call_id 는 int 라 실제 충돌은 없으나
    라우트 순서로 의도를 명확히 한다). LLM 없이 얇게 — service→repository 집계만.
    """
    return pron_svc.get_pronunciation_history(db, member.member_id)


@router.get("/daily-status")
def get_daily_status(
    date: str, member: CurrentMember, db: DbSession, tz_offset: int = 0
) -> dict:
    """'오늘 통화함' 파생 체크(저장 컬럼·일일 초기화 없음 — call 에서 계산).

    - date: 클라이언트 로컬 날짜 "YYYY-MM-DD"(사용자가 '오늘'이라 여기는 날, 필수).
    - tz_offset: 클라이언트 UTC 오프셋(분, 동쪽 +). KST=540. 미지정 시 0(UTC).
    유효 = 그 로컬 하루 안 10초+ 통화(done/analyzing). 응답 {date, called_today}.
    정적 경로라 `/{call_id}` 보다 먼저 선언(라우트 순서로 의도 명확화).
    """
    return CallService(db).daily_status(member.member_id, date, tz_offset)


@router.get("/{call_id}/pronunciation", response_model=PronunciationReport)
async def get_pronunciation_report(
    call_id: int, request: Request, member: CurrentMember
) -> PronunciationReport:
    """통화별 발음 상세 — 문장별 점수 + 소리(alpha)별 집계 + 국가 맞춤 코칭 한마디.

    코칭 한마디는 counted 복습 수 기반 캐시(pron_feedback_n)로 재사용하고, 미스일 때만
    최저 자모로 PronunciationTip LLM 을 1콜 돈다. 자모가 없으면 comment=None(LLM 스킵),
    genai client 미준비/LLM 실패면 기존 캐시(또는 None)로 graceful 폴백.

    ⚠️ async — LLM 호출·백그라운드 캐시 저장이 이벤트 루프를 필요로 한다. DB 는
    svc.run_db(threadpool)로 오프로드한다. 없거나 타인 통화면 404.
    """
    client = getattr(request.app.state, "genai_client", None)
    settings = getattr(request.app.state, "settings", None)
    session_factory = getattr(request.app.state, "session_factory", None)
    if settings is None or session_factory is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "발음 서비스가 준비되지 않았습니다."
        )
    report = await pron_svc.get_pronunciation_report(
        call_id=call_id,
        member_id=member.member_id,
        session_factory=session_factory,
        client=client,
        settings=settings,
    )
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다.")
    return report


@router.get("/{call_id}", response_model=CallDetail)
def get_call(call_id: int, member: CurrentMember, db: DbSession) -> CallDetail:
    """통화 상세(내 통화만, 타인/없는 통화면 404)."""
    return CallService(db).get_call(member.member_id, call_id)


@router.get("/{call_id}/result", response_model=CallResult)
def get_call_result(call_id: int, member: CurrentMember, db: DbSession) -> CallResult:
    """통화 종료 후 결과 화면 — 평가 평균 + 문장 전체."""
    return CallService(db).get_call_result(member.member_id, call_id)


@router.get("/{call_id}/pronunciation-report", response_model=LearningSummaryOut)
async def get_learning_summary_report(
    call_id: int, request: Request, member: CurrentMember
) -> LearningSummaryOut:
    """복습 종료 후 발음 리포트(Flutter LearningSummary) — 통과·문장별·소리별·최근 세션.

    pronunciation_service 의 실데이터(국적·자모별 집계·국가 맞춤 코칭 comment)를 받아
    LearningSummary 형태(통과수·평균·가장 어려웠던 소리·소리별 정확도 2+2 선별·세션 delta)로
    가공한다. 소리·국적·코칭은 전부 실데이터. 통화 없으면 404, 서비스 미준비면 503.

    ⚠️ async — pronunciation_service 가 LLM·백그라운드 캐시로 이벤트 루프를 쓴다. DB 는
    run_db(threadpool)로 오프로드한다.
    """
    client = getattr(request.app.state, "genai_client", None)
    settings = getattr(request.app.state, "settings", None)
    session_factory = getattr(request.app.state, "session_factory", None)
    if settings is None or session_factory is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "발음 서비스가 준비되지 않았습니다."
        )
    return await report_svc.build_learning_summary(
        member.member_id,
        call_id,
        session_factory=session_factory,
        client=client,
        settings=settings,
    )


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
