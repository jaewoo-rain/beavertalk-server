"""CallRepository — 통화 조회/추가/삭제."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.sql.selectable import Exists

from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.models.sentence import Sentence


def _spoke_exists() -> Exists:
    """«학습자가 이 통화에서 최소 한 번 말했다» — has_call_in_window 와 C12(달력)
    «성립 통화» 판정이 같이 쓰는 EXISTS 서브쿼리(정의는 has_call_in_window 참조)."""
    return (
        select(CallRawData.call_raw_data_id)
        .where(
            CallRawData.call_id == Call.call_id,
            CallRawData.role == "user",
            CallRawData.content.isnot(None),
            CallRawData.content != "",
        )
        .exists()
    )


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
        self,
        member_id: int,
        start_utc,
        end_utc,
        call_type: str | None = None,
        exclude_call_type: str | None = None,
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

        두 필터 모드(Q7, 2026-09-24) — 서로 배타적으로 쓴다:
        - call_type: 주면 **그 콜타입만** 센다(일일 한도용 — level_test 와 chat 은
          서로의 한도를 깎지 않는다). `level_test_today` 가 이 모드를 쓴다.
        - exclude_call_type: 주면 **그 콜타입만 빼고 전부** 센다(학습 달력
          `calendar_calls` 와 같은 기준). `called_today`(홈 배지)가 이 모드를 쓴다 —
          예전엔 call_type="chat" 로 정확매칭해 표현학습·프리토킹이 "오늘 통화함"에서
          빠졌다(달력은 이미 "레벨테스트 빼고 전부"를 썼는데 배지만 어긋났었다).
        둘 다 None 이면 전 콜타입.
        """
        inner = select(Call.call_id).where(
            Call.member_id == member_id,
            Call.call_date >= start_utc,
            Call.call_date < end_utc,
            Call.status.in_(("done", "analyzing")),
            _spoke_exists(),
        )
        if call_type is not None:
            inner = inner.where(Call.call_type == call_type)
        if exclude_call_type is not None:
            inner = inner.where(Call.call_type != exclude_call_type)
        return bool(self.db.scalar(select(inner.exists())))

    def sum_total_time_in_window(
        self, member_id: int, start_utc, end_utc, *, exclude_call_types: tuple[str, ...] = (),
    ) -> int:
        """[start_utc, end_utc) 안에 **시작한** 통화의 `total_time` 합(초) — 하루 통화 총량
        예산(C4, 2026-09-23) 집계용.

        ⚠ `has_call_in_window` 와 달리 "학습자가 말했나"(spoke)를 걸지 않는다 — 예산은
          **써버린 시간**을 재는 것이라, 마이크가 안 열린 통화도 Gemini 세션이 열려 있던
          시간만큼 total_time 이 쌓였다면 그 소비가 실제다(옛 count 한도의 "성립" 기준과는
          목적이 다르다). `status` 필터도 없다 — `failed` 로 끝난 조각도 시간은 실제로
          썼다(옛 (done,analyzing,ongoing) 화이트리스트는 실패한 조각을 조용히 공짜로
          만들었다, QA C4 재검-6차).

        ⭐⭐ C4 재설계(2026-09-23, bt-back B) — "진행 중 조각의 경과 시간 추정" 로직을
          **통째로 없앴다.** `mark_fragment_ended`(call_session.py, 끊김을 인지한 그
          자리)가 이제 `fragment_ended_at` 과 `total_time` 을 **같은 쓰기**로 확정하므로,
          이 쿼리가 도는 시점(새 통화를 시작하려는 순간)엔 그 전 조각들의 `total_time`
          이 이미 정확하다 — 살아있는 조각이 있다면애초에 `active_ongoing_call_id`
          (한 사람 한 통화 게이트)가 새 시작 자체를 막으므로, 이 SUM 이 "아직 안 끝난"
          조각을 볼 일이 없다. 그래서 단순 `SUM(total_time)` 이면 충분하다.
        """
        stmt = select(func.coalesce(func.sum(Call.total_time), 0)).where(
            Call.member_id == member_id,
            Call.call_date >= start_utc,
            Call.call_date < end_utc,
        )
        if exclude_call_types:
            stmt = stmt.where(Call.call_type.notin_(exclude_call_types))
        return int(self.db.scalar(stmt) or 0)

    def active_ongoing_call_id(self, member_id: int) -> int | None:
        """이 회원에게 지금 **살아있는 조각**(진행 중인 통화)이 있으면 그 call_id —
        "한 회원은 동시에 한 통화만" 정책의 근거 쿼리(QA C4 재검-③④).

        ⭐ "살아있다" = `fragment_started_at IS NOT NULL AND fragment_ended_at IS NULL
          AND (now - fragment_started_at) <= _ONGOING_ELAPSED_CAP_S`. **`status` 는
          안 본다**(재검-3차와 같은 이유 — 분석 파이프라인의 지연된 status 갱신이 진행
          중 판정에 끼어들면 안 된다). 상한을 넘긴 행은 크래시·강제종료로 못 닫힌
          **죽은 세션**으로 보고 무시한다(안 그러면 죽은 행 하나가 그 회원을 영영
          통화 못 걸게 잠근다).

        ⛔⛔ QA C4 재검-④(2026-09-23): **`exclude_call_id` 파라미터를 없앴다** —
          "살아있는 조각이 있으면 무조건 거절"이 규칙 전체다. 정상 이어하기는 조각
          저장(`finalize_call`/`mark_fragment_ended`)이 `fragment_ended_at` 을 찍은
          **뒤에** 요청이 오므로 이미 살아있지 않다 — 예외를 둘 필요가 없다.
          `exclude_call_id` 로 "자기 자신"만 봐주던 옛 버전은, call_type=chat·
          level_test(둘 다 `resume_call` 을 아예 안 부른다)가 살아있는 **자기 자신의**
          call_id 를 `continues_call_id` 에 실어 보내는 것만으로 이 게이트를 우회하는
          구멍이었다 — 예외 자체를 없애 구조로 막는다.
        ⚠ 행 잠금·예약은 하지 않는다(과한 구조) — 동시에 두 요청이 이 쿼리를 동시에
          통과하는 아주 좁은 경합(둘 다 "살아있는 조각 없음"을 보고 통과)까지는 못
          막는다. 창이 1초 수준이라 감수한다(결정) — 원자화하려면 회원 단위 lease 가
          필요하다.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=_ONGOING_ELAPSED_CAP_S)
        stmt = select(Call.call_id, Call.fragment_started_at).where(
            Call.member_id == member_id,
            Call.fragment_started_at.isnot(None),
            Call.fragment_ended_at.is_(None),
        )
        for call_id, fragment_started_at in self.db.execute(stmt):
            started = fragment_started_at if fragment_started_at.tzinfo else fragment_started_at.replace(tzinfo=timezone.utc)
            if started >= cutoff:
                return call_id
        return None

    def calendar_calls(self, member_id: int, start_utc, end_utc) -> Sequence:
        """C12(2026-09-23) — 학습 달력 집계 대상: [start_utc, end_utc) 안에서 시작한
        **성립 통화**(has_call_in_window 와 같은 기준), 레벨테스트 제외.

        ⛔ N+1 방지(bt-back 조건⑥) — 날짜별로 쪼개 부르지 않는다. 요청 범위 전체를
          **한 번**에 가져와 파이썬에서 로컬 날짜로 묶는다(서비스 계층).
        """
        stmt = select(
            Call.call_id, Call.call_date, Call.total_time, Call.user_word_count,
        ).where(
            Call.member_id == member_id,
            Call.call_date >= start_utc,
            Call.call_date < end_utc,
            Call.call_type != "level_test",
            Call.status.in_(("done", "analyzing")),
            _spoke_exists(),
        )
        return self.db.execute(stmt).all()

    def sentence_counts_by_call(self, call_ids: Sequence[int]) -> dict[int, int]:
        """C12 — 통화별 활성(소프트 삭제 제외) 문장 수. 현지인 표현 짝(kind='native')도
        같은 `call_id` 라 자연히 포함된다(별도 분기 없음)."""
        if not call_ids:
            return {}
        stmt = (
            select(Sentence.call_id, func.count(Sentence.sentence_id))
            .where(Sentence.call_id.in_(call_ids), Sentence.deleted_at.is_(None))
            .group_by(Sentence.call_id)
        )
        return dict(self.db.execute(stmt).all())

    def add(self, call: Call) -> Call:
        self.db.add(call)
        return call

    def delete(self, call: Call) -> None:
        self.db.delete(call)
