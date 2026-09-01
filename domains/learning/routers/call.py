"""call 라우터 — 통화 저장/목록/상세/원본/평점/삭제."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

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
    PronSummaryOut,
    PronHistoryItem,
    PronunciationReport,
)
from domains.learning.schemas.pronunciation_report import LearningSummaryOut
from domains.learning.models.call import Call
from domains.learning.service import normalcall_service as svc
from domains.learning.service import pronunciation_report_service as report_svc
from domains.learning.service import pronunciation_service as pron_svc
from domains.learning.service import call_service
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


@router.get("/pronunciation-summary", response_model=PronSummaryOut)
def get_pronunciation_summary(
    member: CurrentMember,
    db: DbSession,
    sessions: int = Query(10, ge=1, le=50, description="평균에 넣을 최근 통화 수"),
) -> PronSummaryOut:
    """마이페이지 발음 분석 카드 — 최근 N세션 발음 4지표 평균.

    정적 경로라 `/{call_id}` 보다 먼저 선언한다(pronunciation-history 와 같은 이유).

    ⭐ 값의 출처는 **SpeechSuper 실채점**이다(2026-08-28 재연동 확인). 만료됐던 동안
    스텁으로 버티다가, 벤더가 살아나자 이 경로 그대로 진짜 점수가 채워졌다 —
    스키마·계산은 한 줄도 안 바꿨다. 폴백 규율은 그대로 살아 있다(R5).
    """
    return pron_svc.get_pronunciation_summary(db, member.member_id, sessions)


@router.get("/daily-status")
def get_daily_status(
    date: str, member: CurrentMember, db: DbSession, tz_offset: int = 0
) -> dict:
    """⭐ **오늘 더 통화할 수 있나** — 통화를 시작하기 전 클라의 1차 검증.

    - date: 클라이언트 로컬 날짜 "YYYY-MM-DD"(사용자가 '오늘'이라 여기는 날, 필수).
    - tz_offset: 클라이언트 UTC 오프셋(분, 동쪽 +). KST=540. 미지정 시 0(UTC).
      ⚠ 외국인 학습자라 타임존이 제각각이다 — 하루 경계를 서버가 고정하지 않는다.

    ```
    {
      "date": "2026-08-20",
      "called_today":        true,    # 사실 — 오늘 일반 통화를 했나
      "level_test_today":    false,   # 사실 — 오늘 레벨테스트를 했나
      "can_call_normal":     true,    # ⭐ 판정 — 서버가 지금 거절할지
      "can_call_level_test": true,    # ⭐ 판정
      "max_fragments":       1        # ⭐ 이 회원이 이을 수 있는 조각 수(Free 1 / Pro·Max 3)
    }
    ```

    ## ⛔ `called_today` 와 `can_call_normal` 은 다른 축이다
    앞은 **사실**이고 뒤는 **판정**이다. `called_today=true` 일 때 Free 는 못 하고
    Pro 는 할 수 있다 — 같은 사실에서 결론이 반대로 갈린다. 그래서 판정을 클라가
    조합하게 두지 않고 서버가 내린다(서버 거절과 **같은 함수**를 부른다).

    ## ⚠ 지금 `can_call_*` 은 항상 true 다
    `is_daily_limit_reached` 가 `ENV != "prod"` 에서 즉시 False 를 돌려주고,
    app-api 의 ENV 는 `'test'` 다. **버그가 아니라 사실의 반영**이다 — 이 필드의 계약은
    "한도를 계산해 준다"가 아니라 "**서버가 지금 거절할지**"다. 한도를 실제로 켜면
    이 값이 저절로 바뀌고 클라는 고칠 게 없다.

    ## ⛔ `can_call_*` 은 `date` 가 아니라 서버의 **'지금'** 을 본다
    `date` 는 `called_today`·`level_test_today`(사실) 축이 소유한다. 판정 축은
    `is_daily_limit_reached` 가 `datetime.now()` 로 창을 잡는다.
    ⭐ **이게 맞는 동작이다**: 서버 거절은 통화를 거는 **그 순간** 일어나므로, 판정이
      '지금'을 봐야 거절과 같은 답이 된다. `date` 를 따르게 만들면 "어제 날짜로 물으면
      된다고 한다"가 되어 두 값이 갈린다 — 이 엔드포인트가 막으려던 바로 그 사고다.
    ⇒ 클라는 **오늘 날짜로만** 물어야 한다. 과거 날짜 조회는 `called_today` 만 유효하다.

    ## 통화 **중** 연장은 여기가 아니다
    5분 뒤 "이어서" 는 `GET /{call_id}/resume-status` 가 소유한다
    (`ready`·`can_resume`·`fragment_count`·`max_fragments`).
    ⚠ 양쪽 `max_fragments` 는 **같은 함수**(call_fragments_for_member)에서 온다 —
      한쪽만 고치면 시작 화면과 연장 화면이 다른 말을 한다.

    정적 경로라 `/{call_id}` 보다 먼저 선언(라우트 순서로 의도 명확화).
    """
    return CallService(db).daily_status(member.member_id, date, tz_offset)


@router.get("/{call_id}/resume-status")
def get_resume_status(call_id: int, member: CurrentMember, db: DbSession) -> dict:
    """⭐ **이 통화를 지금 이어도 되나** — 클라가 "이어서" 버튼을 열 시점을 정하는 값.

    ## 왜 폴링인가(서버가 밀어주지 않고)
    조각을 끝내는 주체가 **클라**다 — 5분에 소켓을 닫는다. 그 순간 서버는 밀어 줄 통로가
    없다(소켓이 이미 없다). 그래서 클라가 물어본다.

    ## `ready` 가 뜻하는 것
    다음 조각에 넘길 **요약 슬롯이 준비됐다**(`call.resume_context`).
    ⛔ 이게 없을 때 이어하면 서버가 원문 발췌로 폴백하는데, 실측(call 1086) 그때
      **비버가 발췌를 대본으로 읽어 첫 인사를 글자까지 똑같이 반복했다.**
      그래서 사장님 지시(2026-08-19): "요약이 제대로 되어야지만 버튼이 활성화되도록".
    ⚠ 서버에도 즉석 생성 폴백이 있으므로 `ready=false` 에 이어도 **동작은 한다** —
      다만 통화 시작이 그만큼 늦고 품질이 흔들린다. 이 값은 그걸 피하라는 신호다.

    ## `can_resume`
    조각 상한(Free 1 / Pro·Max 3)이 남았나. ⛔ 본인 통화가 아니면 404 다 —
    남의 통화 상태를 조회로 떠보지 못하게 한다.
    """
    call = db.get(Call, call_id)
    if call is None or call.member_id != member.member_id:
        raise HTTPException(status_code=404, detail="통화를 찾을 수 없습니다")
    used = call.fragment_count or 1
    total = call_service.call_fragments_for_member(db, member.member_id)
    return {
        # ⛔ **"있다"가 아니라 "최신인가"** 다(2026-08-19 실측). 조각2 직후에는 조각1 때 만든
        #   요약이 남아 있어 `bool()` 로는 즉시 true 가 뜬다 — 사장님: "두 번째에서는
        #   0.4초 만에 이어하기가 준비됐네." 버튼은 열리는데 정작 이어할 때는 낡은 걸 버리고
        #   즉석 생성을 돌려서, **게이트가 막으려던 지연이 그대로 난다.**
        #   ⚠ `resume_materials` 와 **같은 판정**을 써야 한다(한 함수로 모았다) — 두 곳이
        #     다른 기준을 쓰면 "준비됐다는데 느린" 상태가 계속 산다.
        "ready": svc.resume_context_is_fresh(db, call_id),
        "can_resume": used < total and (call.call_type or "normal") == "normal",
        "fragment_count": used,
        "max_fragments": total,
        # ⚠ 분석이 아직 도는 중인지 — 클라가 "요약 준비 중" 을 보여줄 수 있게.
        "analyzing": (call.status or "") in ("ongoing", "analyzing"),
    }


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
