"""CallRepository — 통화 조회/추가/삭제."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.models.sentence import Sentence


# ⭐ QA C4 재검-②(2026-09-23): ongoing 조각의 경과 추정 상한(초) — 오래 방치된 ongoing
#   (크래시·강제종료로 status 가 안 닫힌 옛 행)이 하루 예산을 통째로 잠그지 않게 한다.
#   ⚠ `realtime.call_session.ABSOLUTE_CALL_TIMEOUT_S`(540s, 통화 절대 백스톱)와 **같은
#     값이어야 한다** — repository 가 routers/realtime 계층을 import 하면 레이어 방향
#     (routers→service→repository)이 뒤집히므로 상수를 따로 두고 값만 맞춘다. 두 값이
#     달라지면 회귀(test_daily_call_budget.py)가 잡는다.
_ONGOING_ELAPSED_CAP_S = 540.0


class CallRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_basic(self, call_id: int) -> Optional[Call]:
        """소유 검증·rating 수정용(연관 미로딩)."""
        return self.db.get(Call, call_id)

    def get_detail(self, call_id: int) -> Optional[Call]:
        """상세용 — 발화(컬렉션)=selectin, 그 안 평가(스칼라)=joined, 캐릭터=joined."""
        return self.db.get(
            Call,
            call_id,
            options=[
                joinedload(Call.character),
                selectinload(Call.sentences).joinedload(Sentence.evaluation),
            ],
        )

    def get_with_raw(self, call_id: int) -> Optional[Call]:
        return self.db.get(Call, call_id, options=[selectinload(Call.raw_data)])

    def list_by_member(
        self, member_id: int, limit: int = 20, offset: int = 0
    ) -> Sequence[Call]:
        stmt = (
            select(Call)
            .where(Call.member_id == member_id)
            .options(joinedload(Call.character))  # 목록엔 캐릭터만(발화 미포함)
            .order_by(Call.call_date.desc(), Call.call_id.desc())
            .limit(limit)
            .offset(offset)
        )
        return self.db.scalars(stmt).all()

    def has_call_in_window(
        self, member_id: int, start_utc, end_utc, call_type: str | None = None
    ) -> bool:
        """[start_utc, end_utc) 안에 **성립한 통화**가 있는지(EXISTS).

        성립 = status in(done, analyzing) AND **학습자가 최소 한 번 말했다**
        (call_raw_data 에 role='user' 이고 전사가 빈 값이 아닌 행이 존재).

        왜 '유저가 말했는가'인가: 옛 기준은 total_time >= 10초 였는데 자의적이었다.
        실측(prod)에서 normal 통화 405건 중 205건이 **학습자 발화 0건**이고, 그중 44건은
        10초를 넘겨 하루를 소모했다(최장 324초 — 비버 혼자 5분을 떠든 통화). 마이크가 안
        열렸거나 듣기만 한 통화가 한도를 깎으면 안 된다.

        선톡(비버가 먼저 거는 첫 발화)은 role='beaver' 라 자동으로 제외된다.

        ⚠ 성립하지 않은 통화도 **행은 남긴다**(삭제하지 않는다). Live 세션을 연 비용은
        이미 나갔으므로 그 증거가 있어야 요금을 설명할 수 있고, 버그 조사 재료이기도 하다.

        call_type: 주면 그 콜타입만 센다(일일 한도용 — level_test 와 normal 은 서로의
            한도를 깎지 않는다). None 이면 전 콜타입.
        """
        spoke = (
            select(CallRawData.call_raw_data_id)
            .where(
                CallRawData.call_id == Call.call_id,
                CallRawData.role == "user",
                CallRawData.content.isnot(None),
                CallRawData.content != "",
            )
            .exists()
        )
        inner = select(Call.call_id).where(
            Call.member_id == member_id,
            Call.call_date >= start_utc,
            Call.call_date < end_utc,
            Call.status.in_(("done", "analyzing")),
            spoke,
        )
        if call_type is not None:
            inner = inner.where(Call.call_type == call_type)
        return bool(self.db.scalar(select(inner.exists())))

    def sum_total_time_in_window(
        self, member_id: int, start_utc, end_utc, *, exclude_call_types: tuple[str, ...] = (),
    ) -> int:
        """[start_utc, end_utc) 안에 **시작한** 통화의 `total_time` 합(초) — 하루 통화 총량
        예산(C4, 2026-09-23) 집계용.

        ⚠ `has_call_in_window` 와 달리 "학습자가 말했나"(spoke)를 걸지 않는다 — 예산은
          **써버린 시간**을 재는 것이라, 마이크가 안 열린 통화도 Gemini 세션이 열려 있던
          시간만큼 total_time 이 쌓였다면 그 소비가 실제다(옛 count 한도의 "성립" 기준과는
          목적이 다르다).
        status 는 (done, analyzing, ongoing) 만 센다 — 아직 저장 안 끝난 ongoing 도 진행
          중인 소비라 빼면, 끊고 바로 또 거는 구멍이 생긴다.

        ⛔⛔ QA C4 재검-②(2026-09-23, 재재검): `ongoing` 인 행은 아직 조각이 끝나지
          않아 이번 조각의 `total_time` 이 반영되지 않는다 — 통화 진행 중(특히 동시
          접속으로 같은 회원이 두 번째 세션을 여는 경합)에는 예산 검사가 "이번 조각은
          아직 0초"로 잘못 통과한다. ⇒ 합계 = `sum(total_time or 0)`(전 상태 공통 —
          done·analyzing 은 이 값만 본다) **+** `status='ongoing'` 인 행마다
          `min(now - fragment_started_at, _ONGOING_ELAPSED_CAP_S)`(이번 조각의 진행 중
          경과 추정, 상한 있음 — 죽은 ongoing 행이 예산을 영영 잠그지 않게).
          ⛔ 경과 추정은 **ongoing 에만** 건다 — done·analyzing 에 걸면(status 를 안
          보고 total_time NULL 만 보던 옛 코드), 이미 끝난 통화인데 total_time 이 NULL
          로 남은 행(예: 분석 실패)이 **쿼리할 때마다 elapsed 가 계속 자라** 예산을
          점점 더 깎는 별개의 버그가 된다.
          ⚠ `fragment_started_at` 이 NULL(마이그레이션 전 옛 ongoing 행)이면 `call_date`
          로 폴백한다(R5 — 모르면 예전 근사치라도 쓴다).
        이 파일은 sqlite(테스트)·postgres(운영) 양쪽에서 돌아야 해서 SQL 레벨
        COALESCE/EXTRACT(EPOCH) 대신 파이썬에서 계산한다 — 회원 하루 통화 수가 적어
        (많아야 몇 건) 성능상 문제가 없다.
        ⚠ 회원 단위 잠금·예약은 만들지 않는다(과한 구조) — 같은 회원이 동시에 두 세션을
          열어 **각각** 경과 시간을 추정하는 경우까지 막으려면 행 잠금이 필요하다. 그
          경로는 동시통화 금지 정책(call_session — 통화 시작 시 진행 중 통화 존재 검사)
          이 애초에 막는다. 여기는 "0으로 새는" 구멍만 막는다.
        """
        stmt = select(
            Call.total_time, Call.status, Call.fragment_started_at, Call.call_date,
        ).where(
            Call.member_id == member_id,
            Call.call_date >= start_utc,
            Call.call_date < end_utc,
            Call.status.in_(("done", "analyzing", "ongoing")),
        )
        if exclude_call_types:
            stmt = stmt.where(Call.call_type.notin_(exclude_call_types))
        now = datetime.now(timezone.utc)
        total = 0
        for total_time, status, fragment_started_at, call_date in self.db.execute(stmt):
            total += total_time or 0
            if status != "ongoing":
                continue
            started = fragment_started_at or call_date
            if started is None:
                continue
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed = max(0.0, (now - started).total_seconds())
            total += int(min(elapsed, _ONGOING_ELAPSED_CAP_S))
        return total

    def active_ongoing_call_id(
        self, member_id: int, *, exclude_call_id: int | None = None,
    ) -> int | None:
        """이 회원의 **살아있는** ongoing 통화 id(있으면) — QA C4 재검-③(2026-09-23),
        "한 회원은 동시에 한 통화만" 정책의 근거 쿼리.

        ⭐ "살아있다" = `fragment_started_at`(없으면 `call_date`)로부터
          `_ONGOING_ELAPSED_CAP_S`(=통화 절대 백스톱과 같은 값) 이내 — 그보다 오래된
          ongoing 은 크래시·강제종료로 status 가 못 닫힌 **죽은 세션**으로 보고 무시한다
          (안 그러면 죽은 행 하나가 그 회원을 영영 통화 못 걸게 잠근다).
        exclude_call_id: 지금 이어하려는 그 통화 id — 자기 자신은 "다른 통화"가 아니다.
        ⚠ 행 잠금·예약은 하지 않는다(과한 구조) — 동시에 두 요청이 동시에 이 쿼리를
          통과하는 아주 좁은 경합까지는 못 막는다. 목적은 "같은 회원이 통화 두 개를
          나란히 켜 놓고 예산을 두 번 쓰는" 흔한 경로를 막는 것이다.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=_ONGOING_ELAPSED_CAP_S)
        stmt = select(Call.call_id, Call.fragment_started_at, Call.call_date).where(
            Call.member_id == member_id,
            Call.status == "ongoing",
        )
        if exclude_call_id is not None:
            stmt = stmt.where(Call.call_id != exclude_call_id)
        for call_id, fragment_started_at, call_date in self.db.execute(stmt):
            started = fragment_started_at or call_date
            if started is None:
                continue
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if started >= cutoff:
                return call_id
        return None

    def add(self, call: Call) -> Call:
        self.db.add(call)
        return call

    def delete(self, call: Call) -> None:
        self.db.delete(call)
