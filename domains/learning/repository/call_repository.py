"""CallRepository — 통화 조회/추가/삭제."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import and_, func, or_, select
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
        also_include_call_id: int | None = None,
    ) -> int:
        """[start_utc, end_utc) 안에 **활동한** 통화의 `total_time` 합(초) — 하루 통화
        총량 예산(C4, 2026-09-23) 집계용.

        ⛔⛔ R2-a(2026-09-24, 프론트 실기기 QA — 자정 걸친 조각 체인이 새 날 예산을 안
        깎는다) — 두 겹으로 막는다. bt-back 재검에서 1차 수정(also_include_call_id
        만)이 "체인을 명시로 이을 때"만 막고 "체인이 끝난 뒤 **전혀 다른 새 통화**를
        건다"(재현: 23:50 체인이 900초 다 쓰고 정상 종료 → 00:10 새 통화 → call_date
        가 어제라 오늘 SUM 이 0 → 오늘 예산이 고스란히 또 열림 → premium 한 시간에
        최대 30분)는 못 막는다는 걸 잡아냈다.

        ① 일반 규칙(모든 호출에 항상 적용) — `call_date` **또는**
        `fragment_started_at`(가장 최근 조각의 시작 시각)이 이 창 안이면 센다.
        `fragment_started_at` 을 쓰는 이유(bt-back 재검 — «그 값이 무엇 때문에
        바뀌는가부터 봐라»): `fragment_ended_at` 은 후보에서 뺐다 — 운영 실측(자정
        걸친 8건 중 7건)이 C4 마이그레이션(`30dda365f616`)의 백필 흔적이었다
        (`fragment_ended_at = updated_at` — 재분석·수정으로 옛 행의 `updated_at` 이
        밀리면 엉뚱한 날짜에 잡힌다). `fragment_started_at` 은 `resume_call`/
        `create_call`(정확히 이 두 곳, grep 으로 확인)에서만 "지금"으로 쓰이고,
        같은 마이그레이션이 옛 행엔 `fragment_started_at = call_date` 로 백필해
        (마이그레이션 소스 확인) **절대 `call_date` 와 다른 값으로 옛 행을 오염시킬
        수 없다** — 이미 `call_date` 조건으로 걸러지는 값이라 안전하다.
        하나의 행은 두 조건 중 하나만 맞아도(또는 둘 다) **한 번만** 더해진다(SQL
        WHERE 는 행 단위 — OR 자체가 이중 계산을 만들지 않는다).

        ② `also_include_call_id`(보조, ①이 못 잡는 좁은 틈 하나를 막는다) — 이어하기
        조각을 열기 **전** 예산 판정에서, 지금 이으려는 그 통화의 call_id 를 넘기면
        추가로 반영한다. ①의 `fragment_started_at` 은 **가장 최근에 끝난 조각의
        시작 시각**이라, 그 조각 자체가 자정을 막 걸쳤으면(예: 23:57 시작·00:03 종료)
        시작 시각은 여전히 "어제"라 ①에 안 걸린다 — 그런데 그 조각이 쓴 시간(활동)은
        오늘에도 걸쳐 있다. ②는 "지금 이 call_id 를 이으려 한다"는 확실한 신호를
        직접 받으므로 타이밍에 무관하게 정확하다. `call_date`/`fragment_started_at`
        가 이미 이 창 안이면(①에 이미 실려 있음) 중복으로 더하지 않는다.
        ⚠ 이중 계산이 아니다 — 이 조정들은 **지금 이 순간의 예산 판정**(daily_budget_
        exceeded/remaining_budget_s, 호출부가 매번 "지금"을 기준으로 새로 계산한다)
        에만 쓰인다. `call_date` 로 집계하는 다른 용도(학습 달력 등)는 그대로 그 통화를
        `call_date` 하루에만 싣는다 — 같은 total_time 이 "어제의 기록"과 "오늘의 잔여
        판정"에 둘 다 나타날 수 있지만, 하루 예산은 **날짜별로 독립된 풀**이라 한 풀에서
        두 번 깎이는 게 아니다(체인이 자정을 걸쳤다는 사실 자체가 두 날 모두에 영향을
        준 것이고, 그걸 각 날의 판정이 각자 반영하는 것이 정확하다).
        운영 실측: 진짜 자정 통과는 1,590건 중 1건(call 198, 6분 차이)뿐이라 ①의
        과다 계상(체인이 두 날 모두에 온전히 잡히는 것)이 실사용자를 부당하게 막을
        위험은 사실상 없다고 판단했다(bt-back 확인).

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
        # ⛔⛔ Q5(2026-09-24) — 이 SUM 은 하드 삭제된 통화 행을 당연히 못 센다(행 자체가
        #   없다) — 삭제 = 예산 환급이다. 일반 회원에게 이게 뚫려 있으면 스스로 하드
        #   삭제를 반복해 하루 예산을 무한 리필하는 구멍이 된다(실측·에이전트 재현).
        #   ⇒ `DELETE /api/v1/calls/{id}` 를 admin 전용(CurrentAdmin)으로 좁혔다(routers/
        #   call.py) — 앱이 이 호출을 아예 안 쓴다(전수 검색 0건). admin 계정은
        #   `is_unlimited_member` 로 애초에 예산 대상이 아니고, 삭제도 **자기 소유
        #   통화만**(_assert_owner) 지울 수 있어 다른 회원 예산을 되돌릴 경로가 없다.
        #   음수 total_time 삽입 구멍은 별도로 막았다(schemas/call.py, ge=0).
        stmt = select(func.coalesce(func.sum(Call.total_time), 0)).where(
            Call.member_id == member_id,
            or_(
                and_(Call.call_date >= start_utc, Call.call_date < end_utc),
                and_(Call.fragment_started_at >= start_utc, Call.fragment_started_at < end_utc),
            ),
        )
        if exclude_call_types:
            stmt = stmt.where(Call.call_type.notin_(exclude_call_types))
        total = int(self.db.scalar(stmt) or 0)
        if also_include_call_id is not None:
            call = self.db.get(Call, also_include_call_id)
            call_date = call.call_date if call is not None else None
            frag_started = call.fragment_started_at if call is not None else None
            # ⚠ sqlite 왕복에서 tzinfo 가 빠질 수 있다(다른 자리들과 같은 방어 —
            #   has_call_in_window·resume_call 의 fragment_started_at 비교 참조).
            if call_date is not None and call_date.tzinfo is None:
                call_date = call_date.replace(tzinfo=timezone.utc)
            if frag_started is not None and frag_started.tzinfo is None:
                frag_started = frag_started.replace(tzinfo=timezone.utc)
            already_counted = (
                (call_date is not None and start_utc <= call_date < end_utc)
                or (frag_started is not None and start_utc <= frag_started < end_utc)
            )
            if (
                call is not None
                and call.member_id == member_id
                and not (exclude_call_types and call.call_type in exclude_call_types)
                and not already_counted
            ):
                total += int(call.total_time or 0)
        return total

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
