"""CallService — 통화 일괄 저장 + 조회/평점/삭제.

핵심: 통화 한 건(call + 발화들 + 발화별 평가 + 원본)을 **한 트랜잭션**으로 저장.
평가는 발화별 1:1 — 발화마다 Evaluation 을 만들어 연결(채점 전이면 점수 NULL placeholder).
"""

from __future__ import annotations

import logging
from datetime import date as _date, datetime, time as _time, timedelta, timezone
from typing import Sequence

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.models.evaluation import Evaluation
from domains.learning.models.sentence import Sentence
from core.config import settings
from domains.learning.repository.call_repository import CallRepository
from domains.learning.schemas.call import (
    CallCharacterBrief,
    CallCreate,
    CallDetail,
    CallResult,
    CallResultSentence,
    CallSummary,
    EvaluationOut,
    RawDataOut,
    ScoreAverage,
    SentenceOut,
)


logger = logging.getLogger(__name__)


def _avg(values: list) -> float | None:
    nums = [v for v in values if v is not None]
    return round(sum(nums) / len(nums), 1) if nums else None


# '오늘 통화함'의 정의는 CallRepository.has_call_in_window 가 소유한다 —
# **학습자가 최소 한 번 말한** done/analyzing 통화. 옛 DAILY_MIN_CALL_S(10초) 기준은
# 폐기했다(자의적이었고, 마이크가 안 열린 통화가 하루를 소모했다).

# ── 일일 통화 한도 ─────────────────────────────────────────────────────── #
# 콜타입별로 **따로** 센다 — 레벨테스트를 했어도 일반 통화 1회가 남는다.
# 근거: docs/20260729_1243_일일-통화-한도-서버-거절.md
DAILY_CALL_LIMIT: dict[str, int] = {"normal": 1, "level_test": 1}

# ⭐⭐ **플랜이 가르는 것은 횟수가 아니라 조각 수다**(2026-08-19 사장님 결정을 반영,
#   2026-08-20 수정). 결정 원문: *"free는 5분 1통화가 제한이고, pro랑 max는 체인으로
#   15분 연달아서가 1통화로 할게."* ⇒ **체인 전체가 '1통화'** 다.
#
#     Free      하루 1통화 × 조각 1개  =  6분
#     Pro·Max   하루 1통화 × 조각 3개  =  18분
#
# ⛔ 왜 뒤늦게 고치나 — **이 표만 옛 계약에 멈춰 있었다.** git 으로 확인:
#     379d654 (3티어 도입)  이 표를 "무제한"으로 박음
#     77ed775 (길이 재편)   길이만 고치고 이 표는 안 건드림
#     08-19   (조각 재편)   조각 축을 만들었는데 역시 이 표는 안 건드림
#   길이 축만 두 번 고치고 **횟수 축을 놔둔 것**이다. 빈 dict(무제한)는 08-04 의 유물이다.
#
# ⛔⛔ **앱 문구가 정면으로 반대다** — `app_en.arb` 에 "Unlimited calls" 가 10군데 있다
#   (planTaglinePro · paywallProSub · bannerGoUnlimitedSub · successProSub …). 결제·성공
#   화면까지 포함이라, 문구가 그대로면 "Unlimited 보고 결제한 사람이 두 번째 통화에서
#   거절"당한다. **문구 변경은 프론트 몫**이다(2026-08-20 사장님: "앱 문구는 프론트쪽에서
#   바꿀거니까 우리는 서버만 생각해").
#   ⚠ 지금 고칠 수 있는 이유: 실측(2026-08-20) 구독 행 2건이 전부 **수동 삽입 max**이고
#     유료구매가 임시차단돼 **진짜 결제자가 없다.** 사람이 붙기 전이 마지막 시점이다.
#
# ⚠ 레벨테스트도 1회로 맞췄다(콜타입을 따로 세므로 일반 1 + 레벨테스트 1). 결정 원문이
#   "1통화"만 말하고 콜타입을 안 갈랐으므로 Free 와 대칭이 기본이다 — 유료는 여러 번
#   보게 하려면 여기 "level_test" 만 빼면 된다(한 줄).
DAILY_CALL_LIMIT_BY_PLAN: dict[str | None, dict[str, int]] = {
    None: DAILY_CALL_LIMIT,    # Free  — 1통화 × 1조각
    "pro": DAILY_CALL_LIMIT,   # Pro   — 1통화 × 3조각(체인 전체가 1통화)
    "max": DAILY_CALL_LIMIT,   # Max   — Pro 상위집합. 차별점은 길이가 아니다
}

# ── 플랜별 통화 길이(초) ───────────────────────────────────────────────── #
# 앱 카피가 이미 계약이다(`app_en.arb`) — 여기 숫자는 그 문구에서 왔다:
#   Free "One 5-minute voice call a day" / Pro "Unlimited calls. 15 minutes each."
# Max 는 "Everything in Pro" + 영상통화라 **길이는 Pro 와 같다**(차별점이 길이가 아니다).
#
# ⛔ 앱 문구와 이 값이 어긋나면 결제한 사람의 통화가 광고보다 짧게 끊긴다 — 이 도메인에서
#   가장 비싼 종류의 불일치라, 문구를 바꿀 땐 이 표도 같이 바꾼다.
# ⚠ 레벨테스트는 이 표에 없다. 3분 하드캡(LEVELTEST_MAX_S)은 측정 설계지 상품 혜택이
#   아니라서 플랜을 타면 안 된다.
# ⭐⭐ **2026-08-19 재편: 길이가 아니라 조각 수가 플랜을 가른다**(사장님 지시).
#   전: Free 한 통화 5분 / Pro·Max 한 통화 15분
#   후: **모두 한 조각 6분**, Free 는 조각 1개 / Pro·Max 는 3개(= 최대 18분)
#   ⇒ 15분 대화가 소켓 하나가 아니라 **6분 조각의 연쇄**로 이루어진다.
#
# ⚠ **6분인 이유**(5분이 아니라): 조각 경계는 **프론트가 정한다** — 5분에 "이어서
#   하시겠습니까?" 를 띄우고 소켓을 닫는다. 서버 시계는 그 뒤에 오는 **백스톱**이라
#   넉넉해야 한다. 5분으로 딱 맞추면 클라 지연·왕복 한 번에 서버가 먼저 끊어
#   비버 말이 잘린다.
# ⚠ 백스톱과의 관계도 확인했다: absolute_timeout = max(540, 360+22+30) = **540 그대로**다.
#   조각을 8분 이상으로 올렸다면 이 관계가 뒤집혔을 것이다.
#
# ⛔⛔ **앱 문구가 아직 옛 계약이다** — `app_en.arb` 의 Pro "Unlimited calls. 15 minutes
#   each." 는 이제 사실이 아니다(한 번에 15분이 아니라 6분×3). 아래 원래 경고가 그대로
#   적용된다: 문구와 값이 어긋나면 결제한 사람의 통화가 광고보다 짧게 끊긴다.
#   ⇒ 프론트에 문구 변경을 요청해야 한다(서버만 고쳐서는 못 닫는 구멍이다).
CALL_FRAGMENT_S = 360.0     # 조각 하나의 서버측 상한(6분) — 플랜 무관

# 플랜별 **조각 수**. 빈 dict 아님 — 모르는 플랜은 Free 로 떨어뜨린다(R5).
CALL_FRAGMENTS_BY_PLAN: dict[str | None, int] = {
    None: 1,     # Free — 6분 한 조각
    "pro": 3,    # 최대 18분
    "max": 3,    # Pro 상위집합 — 길이는 같다(차별점이 길이가 아니다)
}
FREE_CALL_FRAGMENTS = CALL_FRAGMENTS_BY_PLAN[None]

# ⚠ 하위호환: 호출부가 아직 "이 회원의 통화 길이"를 묻는다. 조각 길이가 곧 그 답이다.
CALL_DURATION_S_BY_PLAN: dict[str | None, float] = {
    None: CALL_FRAGMENT_S,
    "pro": CALL_FRAGMENT_S,
    "max": CALL_FRAGMENT_S,
}
FREE_CALL_DURATION_S = CALL_DURATION_S_BY_PLAN[None]


def daily_window_utc(local_date: _date, tz_offset_min: int) -> tuple[datetime, datetime]:
    """클라 로컬 하루 → UTC 반열린 구간 [start, end).

    서버가 UTC 로 경계를 고정하면 한국 사용자는 오전 9시에 날짜가 바뀐다. 외국인
    학습자라 타임존이 제각각이므로 경계는 클라가 알려준 오프셋으로 잡는다
    (daily_status 와 같은 방식 — 두 곳이 어긋나면 "배지는 안 했다는데 서버는 거절"이 된다).
    """
    offset = timedelta(minutes=tz_offset_min)
    start_utc = (datetime.combine(local_date, _time.min) - offset).replace(
        tzinfo=timezone.utc
    )
    return start_utc, start_utc + timedelta(days=1)


def effective_plan(db: Session, member_id: int) -> str | None:
    """지금 **실제로 혜택이 열려 있는** 플랜. Free 면 None.

    판정 본체는 commerce 로 옮겼다(`service/entitlements.py`) — 재료가 구독 행이고,
    캐릭터 잠금 해제도 같은 판정을 쓰기 때문이다. 두 도메인이 각자 규칙을 쓰면
    "통화는 되는데 캐릭터는 잠김" 같은 어긋남이 난다. 여기 이름은 호출부 보존용.
    """
    from domains.commerce.service import entitlements

    return entitlements.effective_plan(db, member_id)


def is_unlimited_member(db: Session, member_id: int) -> bool:
    """이 회원이 **통화 제한을 통째로 면제**받는가 — 지금은 `role == "admin"` 뿐.

    ⭐⭐ 2026-08-20 사장님 지시: *"롤 admin은 (하루 통화 횟수가) 무제한"*.
      ⛔⛔ **횟수 축에만 건다.** 확인 원문: *"1통화 5분씩 3번까지는 동일한데 하루에
        여러번 통화 가능하게 해달라는거야."* ⇒ 한 통화의 **모양**(조각 6분 × 플랜별
        개수)은 admin 도 일반 사용자와 똑같다. 거기까지 특례를 주면 "나는 되는데
        사용자는 안 되는" 길이를 테스트하게 된다.
      ⚠ 그래서 call_fragments_for_member 는 이 함수를 **부르지 않는다**. 의도된 비대칭이니
        "빠뜨렸다"고 보고 추가하지 마라 — 위 지시가 근거다.

    ⛔ 새 축을 만들지 않았다 — `member.role`(user|admin)이 이미 있고 이미 특권 축이다
      (core/deps.get_current_admin 이 /__dev 운영 도구를 그걸로 가른다).
    ⛔ 왜 "구독에 Max 를 꽂는" 방법이 아닌가: 그러면 구독 상태가 active_max 가 되어
      **정작 Free 화면·Free 한도 UI 를 본인이 못 보게 된다.** role 은 구독을 안 건드리므로
      화면은 Free 그대로이고 제한만 안 걸린다 — 테스트용으로 원하는 게 정확히 그것이다.

    ⚠ 조회 실패는 **면제 없음**으로 떨어뜨린다(R5 의 보수 방향). 모르면 제한을 적용하는
      편이 안전하다 — 무제한이 새는 것보다 낫다.
    """
    try:
        from domains.account.models.member import Member as _Member

        role = db.query(_Member.role).filter(_Member.member_id == member_id).scalar()
    except Exception:  # noqa: BLE001 — 롤 조회 실패가 통화를 막으면 안 된다
        logger.warning("통화 제한: 롤 조회 실패 → 면제 없이 진행 member=%s", member_id)
        return False
    return (role or "user") == "admin"


def call_fragments_for_member(db: Session, member_id: int) -> int:
    """이 회원이 한 통화에서 이을 수 있는 **조각 수**. Free 1 / Pro·Max 3.

    ⭐ 2026-08-19 재편의 축이다 — 플랜이 가르는 것은 **길이가 아니라 조각 수**다.
      조각 하나는 누구에게나 6분이고, Pro·Max 는 그걸 3번까지 잇는다.
    ⚠ 실패는 Free(1)로 떨어뜨린다(R5). 모르면 짧게 주는 편이 안전하다.
    """
    # ⛔ **admin 이라고 조각을 더 주지 않는다**(2026-08-20 사장님 확인: "1통화 5분씩
    #   3번까지는 동일한데 하루에 여러번 통화 가능하게"). 면제는 **횟수 축에만** 건다
    #   (is_daily_limit_reached). 한 통화의 모양은 admin 도 일반 사용자와 같아야
    #   테스트가 실사용을 재현한다 — 여기서 특례를 주면 "나는 되는데 사용자는 안 되는"
    #   길이를 보게 된다.
    return CALL_FRAGMENTS_BY_PLAN.get(effective_plan(db, member_id), FREE_CALL_FRAGMENTS)


def call_duration_s_for_member(db: Session, member_id: int) -> float:
    """이 회원의 일반 통화 길이(초) = **조각 하나의 길이**. 전 플랜 6분.

    ⚠ 2026-08-19 이전에는 이 값이 플랜을 갈랐다(Free 5분 / Pro·Max 15분). 지금은
      플랜을 가르는 것이 `call_fragments_for_member`(조각 수)이고 이 값은 상수다.
      ⛔ 그래도 이 함수를 지우지 않는다 — 호출부가 "이 통화 세션이 몇 초짜리냐"를
        묻는 자리는 그대로 남아 있고, 그 답이 곧 조각 길이다.

    ⛔ 일일 한도(is_daily_limit_reached)와 달리 **환경으로 끄지 않는다.** 한도는 켜두면
      개발이 막히지만(하루 1회), 길이는 짧아질 뿐이고 dev 에서 15분을 밟아야 할 땐
      `NORMAL_CALL_DURATION_S` 로 전 회원 강제하는 탈출구가 따로 있다. 여기서 ENV 로
      또 분기하면 "dev 에선 플랜 경로가 한 번도 안 도는" 죽은 코드가 된다.

    실패는 Free 로 떨어뜨린다(R5) — effective_plan 과 같은 방침. 모르면 짧게 주는 편이
    안전하다(길게 줬다 원가가 새는 것보다 낫다).
    """
    return CALL_DURATION_S_BY_PLAN.get(effective_plan(db, member_id), FREE_CALL_DURATION_S)


def is_daily_limit_reached(
    db: Session, member_id: int, call_type: str, tz_offset_min: int = 0
) -> bool:
    """이 회원이 오늘(클라 로컬) 해당 콜타입 한도를 이미 썼는지.

    ⛔ **prod 이거나 `DAILY_LIMIT_ENFORCED` 가 켜졌을 때만 적용한다.** 그 외
    (dev/test/데모)는 자유롭게 쓴다 — 테스트하다 하루가 잠기면 개발이 안 된다.

    ⭐ 스위치를 따로 둔 이유(2026-08-20): `ENV=prod` 는 한도만 켜는 값이 아니다 —
      dev 데모 라우트(main.py:375)와 통화 prod 가드(call_session.py:366)를 같이 켠다.
      프론트가 한도 UI 를 검증하려면 한도만 켜야 했다. 자세한 대가는 config 주석 참조.
    ⚠ prod 는 스위치와 무관하게 계속 돈다 — 실서비스에서 플래그 하나로 한도가 풀리면
      그게 사고다.

    "통화했다"의 정의는 daily_status 와 **같은 것**을 쓴다(학습자 발화 ≥ 1 +
    done/analyzing). 두 곳이 다른 정의를 쓰면 "홈 배지는 안 했다는데 서버는 거절"이 된다.
    마이크가 안 열렸거나 듣기만 한 통화는 한도를 소모하지 않는다.

    남는 구멍: 조용히 5분 세션을 열고 끊으면 한도는 안 깎이는데 Gemini 비용은 나간다.
    한도(상품)와 남용 방지(인프라)는 다른 축이라, 필요해지면 "하루 세션 오픈 N회" 같은
    별도 레이트리밋으로 잡는다.

    tz_offset_min: 클라 UTC 오프셋(분, 동쪽 +). KST=540. 미전송이면 0(UTC).
    """
    if not (settings.DAILY_LIMIT_ENFORCED or settings.ENV == "prod"):
        return False
    # ⭐ admin 은 면제("롤 admin은 모두 무제한" — is_unlimited_member 가 판정을 소유한다).
    #   ⚠ 대가: admin 계정으로는 한도 UI 자체를 테스트할 수 없다(면제니까). 봐야 하면
    #     `POST /__dev/members/{id}/role` 로 잠깐 user 로 내렸다 올리면 된다.
    if is_unlimited_member(db, member_id):
        return False
    limit = DAILY_CALL_LIMIT_BY_PLAN.get(effective_plan(db, member_id), DAILY_CALL_LIMIT).get(
        call_type
    )
    if not limit:
        return False  # 한도가 없는 플랜(유료) 또는 정의되지 않은 콜타입은 막지 않는다
    now_local = datetime.now(timezone.utc) + timedelta(minutes=tz_offset_min)
    start_utc, end_utc = daily_window_utc(now_local.date(), tz_offset_min)
    return CallRepository(db).has_call_in_window(
        member_id, start_utc, end_utc, call_type=call_type
    )


class CallService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = CallRepository(db)

    def create_call(self, member_id: int, data: CallCreate) -> CallDetail:
        call = Call(
            member_id=member_id,
            character_id=data.character_id,
            call_date=data.call_date or datetime.now(timezone.utc),
            total_time=data.total_time,
            summary=data.summary,
            rating=data.rating,
            # 발화별로 Evaluation 을 만들어 연결(1:1). 점수 없으면 placeholder.
            sentences=[
                Sentence(
                    korean_sentence=s.korean_sentence,
                    native_sentence=s.native_sentence,
                    locale=s.locale,
                    voice_url=s.voice_url,
                    is_bookmarked=s.is_bookmarked,
                    evaluation=Evaluation(
                        total_score=s.evaluation.total_score,
                        pronunciation=s.evaluation.pronunciation,
                        fluency=s.evaluation.fluency,
                        rhythm=s.evaluation.rhythm,
                    ),
                )
                for s in data.sentences
            ],
            raw_data=[
                CallRawData(content=r.content, voice_url=r.voice_url, total_time=r.total_time)
                for r in data.raw_data
            ],
        )
        self.repo.add(call)
        self.db.commit()  # call + sentences + evaluations + raw_data 한 트랜잭션
        # 상세 응답을 위해 연관 로딩된 형태로 다시 조회
        return self.get_call(member_id, call.call_id)

    def list_calls(self, member_id: int, limit: int = 20, offset: int = 0) -> list[CallSummary]:
        return [self._to_summary(c) for c in self.repo.list_by_member(member_id, limit, offset)]

    def get_call(self, member_id: int, call_id: int) -> CallDetail:
        call = self.repo.get_detail(call_id)
        self._assert_owner(call, member_id)
        active = [s for s in call.sentences if s.deleted_at is None]  # 소프트 삭제 제외
        return CallDetail(
            **self._summary_fields(call),
            sentences=[self._to_sentence(s) for s in active],
        )

    def get_call_result(self, member_id: int, call_id: int) -> CallResult:
        """통화 종료 후 결과 — 발화 평가들의 평균 + 사용된 문장 전체."""
        call = self.repo.get_detail(call_id)
        self._assert_owner(call, member_id)
        active = [s for s in call.sentences if s.deleted_at is None]  # 소프트 삭제 제외
        evals = [s.evaluation for s in active if s.evaluation]
        average = ScoreAverage(
            total_score=_avg([e.total_score for e in evals]),
            pronunciation=_avg([e.pronunciation for e in evals]),
            fluency=_avg([e.fluency for e in evals]),
            rhythm=_avg([e.rhythm for e in evals]),
        )
        return CallResult(
            call_id=call.call_id,
            summary=call.summary,
            feedback=call.feedback,  # 요구1: 격려 한마디(/result 만 노출)
            rating=call.rating,
            average=average,
            sentences=[CallResultSentence.model_validate(s) for s in active],
        )

    def daily_status(self, member_id: int, local_date: str, tz_offset_min: int) -> dict:
        """'오늘 통화함' 파생 체크 — member 컬럼/일일 초기화 없이 call 에서 계산.

        Args:
            local_date: 클라이언트 로컬 날짜 "YYYY-MM-DD"(사용자가 '오늘'이라 여기는 날).
            tz_offset_min: 클라이언트 UTC 오프셋(분, 동쪽 +). KST=540. 로컬 하루 경계를 UTC 로 환산.

        유효 통화 = 그 로컬 하루 안에 시작 + total_time>=DAILY_MIN_CALL_S + status in(done,analyzing).
        외국인 사용자 타임존이 제각각이라 경계를 서버가 고정하지 않고 클라 로컬 날짜/오프셋으로 받는다.
        """
        try:
            d = _date.fromisoformat(local_date)
        except (ValueError, TypeError):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "date 는 YYYY-MM-DD 형식이어야 합니다."
            )
        if not -14 * 60 <= tz_offset_min <= 14 * 60:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "tz_offset 은 분 단위(-840~840)여야 합니다."
            )
        start_utc, end_utc = daily_window_utc(d, tz_offset_min)
        # 콜타입을 나눠 센다. 한도가 콜타입별로 따로 있으므로(일반 1 + 레벨테스트 1),
        # 합쳐서 세면 레벨테스트만 해도 홈 배지가 "오늘 통화함"이 돼 일반 통화가 아직
        # 남았는데 소진된 것처럼 보인다.
        return {
            "date": local_date,
            "called_today": self.repo.has_call_in_window(
                member_id, start_utc, end_utc, call_type="normal"
            ),
            "level_test_today": self.repo.has_call_in_window(
                member_id, start_utc, end_utc, call_type="level_test"
            ),
            # ⭐⭐ **"했나" 가 아니라 "더 할 수 있나"**(2026-08-20 사장님 지시 —
            #   "프론트단에서 1차 검증하지 않을까?").
            #
            #   ⛔ 왜 클라가 조합하면 안 되나: 위 두 값은 **플랜을 모른다.**
            #     called_today=true 일 때 Free 는 못 하고 Pro 는 할 수 있다 —
            #     같은 응답인데 결론이 반대다. 그 조합을 클라에 맡기면 판정이 두 군데로
            #     갈리고, 어긋나는 순간 "배지는 된다는데 서버가 거절"이 난다.
            #     그 위험은 daily_window_utc 주석에 이미 적혀 있던 것이다.
            #   ⇒ **서버 거절과 똑같은 함수**(is_daily_limit_reached)를 그대로 부른다.
            #     새 정책을 만들지 않는다 — 그래야 두 곳이 영원히 같은 답을 낸다.
            #
            #   ⚠ **지금은 항상 true 다.** is_daily_limit_reached 가 ENV != "prod" 에서
            #     즉시 False 를 돌려주기 때문이다(app-api 의 ENV = 'test'). 버그가 아니라
            #     서버가 실제로 안 막는다는 **사실의 반영**이다 — 이 필드의 계약은
            #     "한도를 판정해 준다"가 아니라 "**서버가 지금 거절할지**"다.
            "can_call_normal": not is_daily_limit_reached(
                self.db, member_id, "normal", tz_offset_min
            ),
            "can_call_level_test": not is_daily_limit_reached(
                self.db, member_id, "level_test", tz_offset_min
            ),
            # ⭐ 통화를 **시작하기 전에** 조각 수를 알려준다. 연장 UI 는 5분 뒤에 뜨는데,
            #   그때 처음 알면 "이 사람이 이을 수 있는 회원인가"를 늦게 알게 된다.
            #   ⚠ resume-status 의 max_fragments 와 **같은 함수**다(call_fragments_for_member).
            #     한쪽만 고치면 시작 화면과 연장 화면이 다른 말을 한다.
            "max_fragments": call_fragments_for_member(self.db, member_id),
        }

    def get_raw(self, member_id: int, call_id: int) -> list[RawDataOut]:
        call = self.repo.get_with_raw(call_id)
        self._assert_owner(call, member_id)
        return [RawDataOut.model_validate(r) for r in call.raw_data]

    def update_rating(self, member_id: int, call_id: int, rating: int) -> CallSummary:
        call = self.repo.get_detail(call_id)
        self._assert_owner(call, member_id)
        call.rating = rating
        self.db.commit()
        self.db.refresh(call)
        return self._to_summary(call)

    def delete_call(self, member_id: int, call_id: int) -> None:
        call = self.repo.get_basic(call_id)
        self._assert_owner(call, member_id)
        self.repo.delete(call)  # sentences/raw/evaluation 은 CASCADE
        self.db.commit()

    # ── 내부 ──
    def _assert_owner(self, call: Call | None, member_id: int) -> None:
        if call is None or call.member_id != member_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다.")

    def _summary_fields(self, call: Call) -> dict:
        return dict(
            call_id=call.call_id,
            call_date=call.call_date,
            total_time=call.total_time,
            summary=call.summary,
            rating=call.rating,
            character=CallCharacterBrief(
                character_id=call.character.character_id,
                name=call.character.name,
                image_url=call.character.image_url,
            ),
        )

    def _to_summary(self, call: Call) -> CallSummary:
        return CallSummary(**self._summary_fields(call))

    def _to_sentence(self, s: Sentence) -> SentenceOut:
        return SentenceOut(
            sentence_id=s.sentence_id,
            korean_sentence=s.korean_sentence,
            native_sentence=s.native_sentence,
            locale=s.locale,
            voice_url=s.voice_url,
            is_bookmarked=s.is_bookmarked,
            evaluation=EvaluationOut.model_validate(s.evaluation) if s.evaluation else None,
        )
