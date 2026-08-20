"""mastery 조회 쿼리 — 체크판·증거·레벨업 게이트·통화 시작 선별의 순수 SELECT (commit 금지).

기존 repository 들은 클래스+Depends 패턴이지만, 이 모듈은 normalcall 통화후 분석의
run_db 클로저(함수형 흐름)에서 mastery_service 와 조합되므로 normalcall_service 와
같은 "db: Session 을 첫 인자로 받는 모듈 함수" 스타일을 따른다(짧은 세션 단위 호출).

승급 게이트는 문법(+L1 청크) 전용 — D12: list_gate_items 만 어휘를 제외하고,
어휘 추적·복습 선별·grandfathering 쿼리는 무변경(is_core 는 가르치는 순서 우선순위).
통화 수 파생값(브리지·버벅임·승급 멘트·G4 창)은 전부 "증거통화"(item_evidence 에
행이 있는 distinct call) 기반 — D15 로 call.is_valid_call 컬럼 캐시를 폐지했다.

설계: docs/20260709_1346_level-system-detailed-mechanics.md ①~③·⑤~⑨,
      docs/20260709_1231_level-system-master-plan.md §5 + 결정 D12·D15.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import and_, case, func, not_, or_, select
from sqlalchemy.orm import Session, aliased

from domains.account.models.member import Member
from domains.learning.models.item_evidence import ItemEvidence
from domains.learning.models.learning_item import LearningItem
from domains.learning.models.member_item_progress import MemberItemProgress
from domains.learning.models.member_language_level import MemberLanguageLevel
from domains.learning.models.member_level_history import MemberLevelHistory

logger = logging.getLogger(__name__)

# 문법 게이트 분모 상한 — min(교재 문법 수, 45). 초과분은 '선택 문법'(게이트 제외,
# 추적·재활용 풀 잔류). 마스터 플랜 §2 / mechanics 파라미터 총괄표.
GRAMMAR_GATE_CAP = 45

# 검출 후보 상한(mechanics ⑤ — 주입 ~12 + practicing 18, 실측 후 50까지 튜닝 가능)
DEFAULT_PRACTICING_CANDIDATES = 18
DEFAULT_INTRODUCED_CANDIDATES = 12
CANDIDATE_CAP = 30


# --------------------------------------------------------------------------- #
# 레벨 출처 접근자 (멀티랭귀지 — member.korean_level → member_language_level 전환)
# --------------------------------------------------------------------------- #
# member.korean_level(단일 스칼라)을 member_language_level(언어별 1행)로 일반화한다.
# ko 는 두 소스를 dual-read/write 로 정합 유지 — 기존 한국어 회원은 mll 행 없이
# korean_level 만 있으므로 폴백 경로가 바이트 불변을 보장한다(하위호환).
def get_language_level(
    db: Session, member_id: int, language: str = "ko"
) -> Optional[int]:
    """회원×언어의 현재 레벨 — member_language_level 행 우선, ko 는 korean_level 폴백.

    - mll 행이 있으면 그 level_no(NULL=콜드스타트)가 진실(폴백하지 않는다).
    - 행 자체가 없을 때만 ko 한정으로 member.korean_level 을 폴백 조회한다(하위호환).
    - 그 외 언어는 행 부재 = None(콜드스타트 → 언어별 레벨테스트 필요).
    """
    row = db.scalar(
        select(MemberLanguageLevel).where(
            MemberLanguageLevel.member_id == member_id,
            MemberLanguageLevel.language == language,
        )
    )
    if row is not None:
        return row.level_no
    if language == "ko":
        member = db.get(Member, member_id)
        return member.korean_level if member is not None else None
    return None


def get_language_level_for_update(
    db: Session, member_id: int, language: str = "ko"
) -> Optional[int]:
    """get_language_level 의 FOR UPDATE 판 — 레벨업 동시 판정 경합 방지(sqlite no-op).

    mll 행이 있으면 그 행을, 없으면(ko 폴백) member 행을 잠근다. 값 해석은 동일.
    """
    row = db.scalar(
        select(MemberLanguageLevel)
        .where(
            MemberLanguageLevel.member_id == member_id,
            MemberLanguageLevel.language == language,
        )
        .with_for_update()
    )
    if row is not None:
        return row.level_no
    if language == "ko":
        member = db.scalar(
            select(Member).where(Member.member_id == member_id).with_for_update()
        )
        return member.korean_level if member is not None else None
    return None


def upsert_language_level(
    db: Session, member_id: int, language: str, level_no: int
) -> None:
    """회원×언어의 현재 레벨을 기록한다(commit 은 호출부 — R3).

    member_language_level 행을 upsert 하고, ko 는 member.korean_level 도 함께 갱신한다
    (dual-write 폴백 — 기존 korean_level 읽기 지점과 정합 유지, 하위호환).
    """
    row = db.scalar(
        select(MemberLanguageLevel).where(
            MemberLanguageLevel.member_id == member_id,
            MemberLanguageLevel.language == language,
        )
    )
    if row is None:
        db.add(
            MemberLanguageLevel(
                member_id=member_id, language=language, level_no=level_no
            )
        )
    else:
        row.level_no = level_no
    if language == "ko":
        member = db.get(Member, member_id)
        if member is not None:
            member.korean_level = level_no


# --------------------------------------------------------------------------- #
# 검출 후보 (⑤ 2단계 — 후보 30 구성)
# --------------------------------------------------------------------------- #
def first_example(item: LearningItem) -> Optional[str]:
    """예문 1개 — examples/gen_examples(JSON 배열 문자열) 첫 항목, 없으면 None.

    검출 후보 표와 공부/유도 블록(persona ex 슬롯)이 공유한다(문법=교재 예문 첫 원소,
    어휘=생성 예문 첫 원소 — 두 컬럼을 순서대로 폴백).
    """
    for raw in (item.examples, item.gen_examples):
        if not raw:
            continue
        try:
            arr = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(arr, list) and arr:
            first = arr[0]
            text = first if isinstance(first, str) else str(first)
            return text.strip()[:60] or None
    return None


def to_candidate(item: LearningItem, *, injected: bool = False) -> dict:
    """LearningItem → 검출 후보 dict({item_id, kind, surface, example, injected}).

    injected=True 는 이번 통화 프롬프트 주입 항목(공부 10+유도) — fast-track 조건 ④
    (선발화 관측)의 판정 재료가 된다.
    """
    return {
        "item_id": item.item_id,
        "kind": item.kind,
        "surface": item.surface,
        "example": first_example(item),
        "injected": injected,
    }


def load_default_candidates(
    db: Session,
    member_id: int,
    *,
    practicing_limit: int = DEFAULT_PRACTICING_CANDIDATES,
    introduced_limit: int = DEFAULT_INTRODUCED_CANDIDATES,
    cap: int = CANDIDATE_CAP,
    language: str = "ko",
) -> list[dict]:
    """검출 후보 기본 구성 — practicing 오래된 순 18 + introduced 최신 12 (상한 30).

    c2(프롬프트 주입) 전이므로 "오늘 주입 ~12" 자리를 introduced 최신분으로 채운다.
    c2 가 주입 항목을 analyze_call 의 candidates 인자로 넘기기 시작하면 이 함수는
    폴백(미전달 시)으로만 쓰인다. injected 는 전부 False(미주입 취급 — fast-track ④).
    (멀티랭귀지) learning_item.language 로 대상 언어 후보만 선별.
    """
    # 주력: practicing 을 last_used_at 오래된 순(미사용 NULL 최우선)으로 — 가장 위태로운 순.
    practicing = db.scalars(
        select(LearningItem)
        .join(MemberItemProgress, MemberItemProgress.item_id == LearningItem.item_id)
        .where(
            MemberItemProgress.member_id == member_id,
            MemberItemProgress.status == "practicing",
            LearningItem.language == language,
        )
        .order_by(
            MemberItemProgress.last_used_at.asc().nulls_first(),
            MemberItemProgress.progress_id.asc(),
        )
        .limit(practicing_limit)
    ).all()

    seen = {i.item_id for i in practicing}
    introduced = [
        i
        for i in db.scalars(
            select(LearningItem)
            .join(MemberItemProgress, MemberItemProgress.item_id == LearningItem.item_id)
            .where(
                MemberItemProgress.member_id == member_id,
                MemberItemProgress.status == "introduced",
                LearningItem.language == language,
            )
            .order_by(
                MemberItemProgress.last_seen_at.desc(),
                MemberItemProgress.progress_id.desc(),
            )
            .limit(introduced_limit)
        ).all()
        if i.item_id not in seen
    ]

    items = (practicing + introduced)[:cap]
    return [to_candidate(i, injected=False) for i in items]


# --------------------------------------------------------------------------- #
# 통화 시작 선별 (① 3-b~e — 공부 30 / 대화 유도 5 / 아는 문법 / 브리지 / 승급 멘트)
# --------------------------------------------------------------------------- #
# 공부 로드(mechanics ② — 본편 5 + 예비 25 = 30) + 밴드별 본편 구성 상한.
#
# ⭐ 2026-08-04: 예비를 5 → 25 로 늘렸다(D4 의 "10" 을 뒤집는다). 15분 통화 실기기 실측에서
#   항목 10개가 **약 4분에 소진**됐고, 그 뒤 비버가 갈 데가 없어 "없으면 오늘은 여기까지
#   마무리할까?"로 미끄러졌다(통화 3분 55초 지점부터 반복). 지시문에 "예비까지 끝나면 응용
#   대화로 이어가라"가 있지만 실효가 없었다 — 재료를 주는 편이 확실하다.
#
# ⛔ 본편(STUDY_MAIN_TOTAL)은 건드리지 마라. 밴드별 구성(신규 문법 정확히 1개 — 항목당
#   75~90초, L1 청크 3, 중급 어휘 상한 3)이 학습 설계라, 늘리면 한 통화에 새 문법이 여럿
#   들어가 소화가 안 된다. 예비는 전부 어휘라 가볍게 넘어갈 수 있어 늘려도 안전하다.
#
# 비용: 항목 20개 추가 ≈ 지시문 +600 토큰. 지시문은 매 턴 재처리되지만 텍스트 단가라
#   15분 통화 기준 +$0.02 수준으로 무시 가능하다. 그리고 지시문은 컨텍스트 압축에
#   밀리지 않으므로(sliding window 는 system_instruction 을 건너뛴다) 통화 내내 유지된다.
#
# ⭐ 2026-08-16: **L1(생존회화)만은 예비도 청크다**(사장님 지시 "레벨 1일때만 chunk로 10개
#   준비해"). 이유는 재료가 실제로 0개였기 때문이다 — 커리큘럼 어휘·문법은 level_no 2 부터
#   시작하고(`assets/level/curriculum_v2/vocab.json` 최소 2), `_pick_new_items` 는
#   `level_no == level_no` 정확일치라 L1 회원의 예비 25(어휘)는 **항상 빈 리스트**였다.
#   그래서 L1 은 본편 3~4개만 들고 통화에 들어갔다. L1 에 존재하는 재료는 청크 46 뿐이므로
#   남은 자리를 청크로 채운다(총 SURVIVAL_STUDY_TOTAL 개). ⛔ 본편 구성은 그대로다.
#   ⭐ 같은 날 10 → **30** 으로 올렸다(사장님 지시 "1단계도 chunk 30개로"). 다른 밴드의
#   총량(본편 5 + 예비 25 = 30)과 같은 수다 — L1 만 적게 줄 이유가 없다. 재료는 청크 46개라
#   충분하고, 모자라면 짧아진다(R5). 지시문 비용은 항목 20개 ≈ +600 토큰(위 계산과 동일).
STUDY_MAIN_TOTAL = 5
STUDY_RESERVE_TOTAL = 25
SURVIVAL_STUDY_TOTAL = 30     # ⭐ L1 전용 — 본편+예비 합계(복습 포함). 전부 청크.
_SURVIVAL_CHUNKS = 3          # L1 본편 = 청크 3 + 어휘 1(문법 0)
_SURVIVAL_VOCAB_CAP = 1
_INTERMEDIATE_VOCAB_CAP = 3   # 중급 본편 어휘 상한 3

# 제외 필터(mechanics ② 표): SEL2 = 자발 1+ && 오류 0 && 최근 14일 사용 → 공부 제외,
# SEL3 = 직전 주입 성공 → 2통화 쿨다운(F 였으면 즉시 재출제).
SEL2_RECENT_USE_DAYS = 14
SEL3_COOLDOWN_CALLS = 2

# 대화 유도 5(mechanics ③): practicing 3 + mastered 최고령 1 + 최근 7일 introduced 1.
CHAT_TARGET_TOTAL = 5
CHAT_PRACTICING_SLOTS = 3
CHAT_INTRODUCED_RECENT_DAYS = 7

KNOWN_GRAMMAR_CAP = 40        # 대화 모드 "아는 문법" soft 범위 상한

# 브리지/버벅임 복습 비중(mechanics ⑨ — 필드 없음, 선별 시점 파생 계산).
# D15: 통화 수 파생값은 전부 "증거통화"(item_evidence 에 행이 있는 distinct call) 기반.
BRIDGE_REVIEW_RATIO = 0.7     # 브리지/소프트 강등 믹스(복습 70%)
NORMAL_REVIEW_RATIO = 0.3     # 정상 믹스(복습 30%)
_BRIDGE_ENTRY_EVIDENCE_CALLS = 3    # 진입 후 증거통화 <3 → 무조건 브리지
_STRUGGLE_ENTRY_EVIDENCE_CALLS = 5  # 소프트 강등 판정 창(진입 5통화 이내)
_STRUGGLE_WINDOW = 3                # 최근 3 증거통화 연속 버벅임
_STRUGGLE_F_RATIO = 0.5


# (멀티랭귀지) 언어별 밴드 경계 룩업 — [(상한 level_no, 밴드명)] 오름차순, 초과분=advanced.
# 지금은 ko 만 실값(현재값 그대로), 나머지 언어는 ko 경계를 재사용한다(_BAND_BOUNDARIES.get
# 폴백). 새 언어가 다른 경계를 쓰려면 여기 1행만 추가하면 된다(코드 분기 없음).
_BAND_BOUNDARIES: dict[str, tuple[tuple[int, str], ...]] = {
    "ko": ((1, "survival"), (5, "beginner"), (9, "intermediate")),
}
_DEFAULT_BAND_KEY = "ko"


def band_of(level_no: int, language: str = "ko") -> str:
    """레벨(1~13)→밴드 — mastery_service._band_of 와 동일 경계(ko).

    (repository→service 역임포트가 순환이라 여기 중복 정의 — 경계 변경 시 양쪽 동시 수정.)
    (멀티랭귀지) 언어별 경계는 _BAND_BOUNDARIES — 미등록 언어는 ko 경계 재사용.
    """
    boundaries = _BAND_BOUNDARIES.get(language, _BAND_BOUNDARIES[_DEFAULT_BAND_KEY])
    for ceil_no, name in boundaries:
        if level_no <= ceil_no:
            return name
    return "advanced"


def _sel3_cooldown_item_ids(
    db: Session, member_id: int, language: str = "ko"
) -> set[int]:
    """SEL3 쿨다운 대상 — 최근 2통화(증거 기준)에서 성공(E1+)했고 F 없는 항목.

    F 가 있으면 "즉시 재출제"라 제외하지 않는다(성공-F 차집합).
    (멀티랭귀지) member-only 집계 — item_evidence.language 로 대상 언어만 필터(오염 차단).
    """
    recent_calls = list(
        db.scalars(
            select(ItemEvidence.call_id)
            .where(
                ItemEvidence.member_id == member_id,
                ItemEvidence.language == language,
            )
            .group_by(ItemEvidence.call_id)
            .order_by(ItemEvidence.call_id.desc())
            .limit(SEL3_COOLDOWN_CALLS)
        ).all()
    )
    if not recent_calls:
        return set()
    rows = db.execute(
        select(ItemEvidence.item_id, ItemEvidence.grade_final).where(
            ItemEvidence.member_id == member_id,
            ItemEvidence.language == language,
            ItemEvidence.call_id.in_(recent_calls),
        )
    ).all()
    success = {i for i, g in rows if g in ("E1", "E2", "E3")}
    failed = {i for i, g in rows if g == "F"}
    return success - failed


def _pick_new_items(
    db: Session,
    member_id: int,
    level_no: int,
    kind: str,
    limit: int,
    *,
    exclude_ids: set[int],
    language: str = "ko",
) -> list[LearningItem]:
    """신규 풀 선별 — 미학습(행 없음) + 미소화 이월(introduced)만, SEL2 제외.

    "미학습"은 practicing/mastered 미도달을 뜻한다(SEL1 은 이 정의로 자연 충족).
    미소화(introduced) 이월분이 선두("다음 통화 선두로 이월"), 이후 교재/우선순위 순 —
    vocab 은 is_core && priority_rank, grammar/chunk 는 seq_no(단원 순서).
    exclude_ids = SEL3 쿨다운 등 호출부 제외 집합.
    """
    if limit <= 0:
        return []
    prog = aliased(MemberItemProgress)
    # SEL2(선발화 관측 — 이미 아는 티): introduced && 자발 1+ && 오류 0 && 최근 14일 사용.
    # last_used_at NULL 은 is_not(None) 가드로 and_ 전체가 FALSE → NOT sel2 = TRUE(잔류).
    recent = datetime.now(timezone.utc) - timedelta(days=SEL2_RECENT_USE_DAYS)
    sel2 = and_(
        prog.spontaneous_count > 0,
        prog.miss_count == 0,
        prog.last_used_at.is_not(None),
        prog.last_used_at >= recent,
    )
    stmt = (
        select(LearningItem)
        .outerjoin(
            prog,
            and_(prog.item_id == LearningItem.item_id, prog.member_id == member_id),
        )
        .where(
            LearningItem.language == language,
            LearningItem.level_no == level_no,
            LearningItem.kind == kind,
            or_(
                prog.progress_id.is_(None),
                and_(prog.status == "introduced", not_(sel2)),
            ),
        )
    )
    if exclude_ids:
        stmt = stmt.where(LearningItem.item_id.not_in(list(exclude_ids)))
    carry_first = case((prog.progress_id.is_not(None), 0), else_=1)  # 이월분 선두
    if kind == "vocab":
        stmt = stmt.where(LearningItem.is_core.is_(True)).order_by(
            carry_first,
            LearningItem.priority_rank.asc().nulls_last(),
            LearningItem.item_id.asc(),
        )
    else:
        stmt = stmt.order_by(
            carry_first,
            LearningItem.seq_no.asc().nulls_last(),
            LearningItem.item_id.asc(),
        )
    return list(db.scalars(stmt.limit(limit)).all())


def _pick_review_items(
    db: Session, member_id: int, level_no: int, *, limit: int, prefer_previous: bool,
    language: str = "ko", exclude_ids: set[int] | frozenset[int] = frozenset(),
) -> list[LearningItem]:
    """복습 선별 — practicing(+미확정 fast-track) && level_no<=내레벨, 오래된 순.

    감쇠점수 정렬은 P3 — P0 정책은 "감쇠 없이 오래된 순". 브리지/버벅임
    (prefer_previous=True)이면 이전 레벨(level_no<k) 항목을 먼저 채운다(⑨ 비중 확대).
    (멀티랭귀지) learning_item.language 로 대상 언어 항목만.

    ## ⭐ 2026-08-16: **미확정 fast-track 을 복습 풀에 넣는다** (버그 수정)
    fast-track 승격은 status=MASTERED 로 올리되 `fast_track_confirmed_at` 이 NULL 인
    **미확정** 상태를 남긴다. 그런데 그 항목은 신규 풀에선 mastered 라 빠지고, 복습
    풀에선 practicing 이 아니라 빠지고, 승급 게이트 G2 에서도 미확정이라 빠졌다 —
    **세 곳 어디에도 없는 유령**이었다.
    ⛔ 설계는 재노출을 전제한다: `FAST_TRACK_FAIL_EXPOSURES = 2`("노출 기회 2회 F만 →
      PRACTICING 복귀")·14일 무F 자동확정. 한 번도 안 물어보면 "14일간 F 없음"이
      **공허하게 참**이 된다 — 틀릴 기회조차 없었으니까. 재노출이 있어야 자동확정이
      비로소 검사가 된다.
    ⚠ **확정된** mastered 는 여기 넣지 마라 — 리텐션 불시 점검은 pick_chat_targets 몫이다.
    ⚠ placement 행은 provenance 조건으로 자연 배제된다(fast_track 만 추가로 허용).
    """
    if limit <= 0:
        return []

    reviewable = or_(
        MemberItemProgress.status == "practicing",
        and_(
            MemberItemProgress.status == "mastered",
            MemberItemProgress.provenance == "fast_track",
            MemberItemProgress.fast_track_confirmed_at.is_(None),
        ),
    )

    def fetch(level_cond, n: int) -> list[LearningItem]:
        if n <= 0:
            return []
        stmt = (
            select(LearningItem)
            .join(
                MemberItemProgress,
                MemberItemProgress.item_id == LearningItem.item_id,
            )
            .where(
                MemberItemProgress.member_id == member_id,
                reviewable,
                LearningItem.language == language,
                level_cond,
            )
        )
        if exclude_ids:
            stmt = stmt.where(LearningItem.item_id.not_in(list(exclude_ids)))
        stmt = stmt.order_by(
            MemberItemProgress.last_used_at.asc().nulls_first(),
            MemberItemProgress.progress_id.asc(),
        ).limit(n)
        return list(db.scalars(stmt).all())

    if prefer_previous and level_no > 1:
        prev = fetch(LearningItem.level_no < level_no, limit)
        return prev + fetch(LearningItem.level_no == level_no, limit - len(prev))
    return fetch(LearningItem.level_no <= level_no, limit)


def pick_study_items(
    db: Session,
    member_id: int,
    level_no: int,
    *,
    review_slots: int,
    bridge_prev_ratio: float,
    language: str = "ko",
    # ⭐ 이어하기 체인의 call_id. 조각들이 같은 행을 쓰므로 이 하나로 "이 통화에서 다뤘나"가
    #   정확히 표현된다. None(첫 조각)이면 아무것도 안 붙는다.
    chain_call_id: int | None = None,
) -> list[dict]:
    """공부 모드 로드 30(본편 5+예비 25)을 선별한다(mechanics ② — 순수 SELECT).

    review_slots(복습 슬롯 수)는 호출부가 밴드·브리지(⑨)로 결정해 넘긴다 —
    브리지/버벅임 시 확대. bridge_prev_ratio>=0.5 면 복습을 이전 레벨 항목부터 채운다.

    밴드별 본편 구성: L1 = 청크 3+어휘 1(문법 0) / 초급 = 복습 0~2+문법 1+어휘 2~4 /
    중급 = 어휘 상한 3 / 고급 = 복습 0~1+문법 1+어휘 3~4. 예비 25 는 전부 어휘(다음 순번).
    ⭐ L1(survival)만 예외 — 예비도 청크이고, 복습 포함 **총 SURVIVAL_STUDY_TOTAL 개**로
    맞춘다(L1 커리큘럼에 어휘·문법이 없어 예비 어휘가 늘 0개였다 — 상수 주석 참조).
    제외 필터: SEL1(MASTERED — 신규 풀이 미학습 한정이라 자연 충족) / SEL2 / SEL3.

    Returns:
        [{"slot": "main"|"reserve", "study_kind": "grammar"|"vocab"|"chunk",
          "state": "new"|"again"|"review",   # ⭐ 유형과 **별개 축**(2026-08-17)
          "item": LearningItem}] — 본편(복습→[L1 청크|문법]→어휘)→예비 순서. 시드/진행
        데이터가 부족하면 짧아지거나 빈 리스트(호출부가 None 처리 — R5).
    """
    band = band_of(level_no, language)
    cooldown = _sel3_cooldown_item_ids(db, member_id, language)

    # ⛔⛔ **쿨다운을 복습에도 건다**(2026-08-20 실측 사고). 함수는 `exclude_ids` 를 받게
    #   되어 있는데(:398) 호출부가 **안 넘기고 있었다.** 신규 풀 3곳(청크·문법·어휘)에는
    #   전부 넘기면서 복습만 빠졌다.
    #   ⇒ 방금 성공한 항목이 다음 통화에 **또 최상단**으로 온다. 정렬이 "오래된 순"이라도
    #     practicing 이 34개뿐이면 금방 다시 1등이 되기 때문이다.
    #   실측(member 20): `안녕히 가세요` 를 **9번** 가르쳤다(반복=9 · 자발=0 · score 5.5).
    #     사장님: "지금 계속 동일한 거 가르치는 거 같은데 기분 탓인가?" — 기분 탓이 아니었다.
    #   ⚠ 쿨다운은 "최근 2통화에서 성공(E1+)했고 F 없는 항목"이다. 틀린 항목은 그대로
    #     다시 나온다(즉시 재출제) — 막는 것은 **맞힌 것의 반복**뿐이다.
    reviews = _pick_review_items(
        db, member_id, level_no,
        limit=review_slots, prefer_previous=bridge_prev_ratio >= 0.5,
        language=language, exclude_ids=cooldown,
    )
    out: list[dict] = [{"slot": "main", "study_kind": i.kind, "item": i} for i in reviews]

    survival_reserve: list[LearningItem] = []
    if band == "survival":
        # ⭐ L1 은 본편·예비를 **한 번에** 뽑아 슬라이싱한다(어휘 경로와 같은 모양) —
        #   두 번 뽑으면 같은 청크가 본편·예비에 겹칠 수 있다. 복습 항목은 practicing 이고
        #   신규 풀은 progress 없음/introduced 라 서로 겹치지 않는다.
        chunks = _pick_new_items(
            db, member_id, level_no, "chunk", SURVIVAL_STUDY_TOTAL,
            exclude_ids=cooldown, language=language,
        )
        out += [
            {"slot": "main", "study_kind": "chunk", "item": i}
            for i in chunks[:_SURVIVAL_CHUNKS]
        ]
        survival_reserve = chunks[_SURVIVAL_CHUNKS:]
    else:
        # 신규 문법 정확히 1개(2개 금지 — 항목당 75~90초).
        grammars = _pick_new_items(
            db, member_id, level_no, "grammar", 1,
            exclude_ids=cooldown, language=language,
        )
        out += [{"slot": "main", "study_kind": "grammar", "item": i} for i in grammars]

    vocab_want = STUDY_MAIN_TOTAL - len(out)
    if band == "survival":
        vocab_want = min(vocab_want, _SURVIVAL_VOCAB_CAP)
    elif band == "intermediate":
        vocab_want = min(vocab_want, _INTERMEDIATE_VOCAB_CAP)
    vocab_want = max(0, vocab_want)

    # 예비: L1 은 청크(위에서 이미 뽑아 뒀다), 그 외 밴드는 어휘 25.
    reserve_want = 0 if band == "survival" else STUDY_RESERVE_TOTAL

    vocabs = _pick_new_items(
        db, member_id, level_no, "vocab",
        vocab_want + reserve_want, exclude_ids=cooldown, language=language,
    )
    out += [{"slot": "main", "study_kind": "vocab", "item": i} for i in vocabs[:vocab_want]]
    out += [
        {"slot": "reserve", "study_kind": "vocab", "item": i}
        for i in vocabs[vocab_want : vocab_want + reserve_want]
    ]

    if band == "survival":
        # 총 SURVIVAL_STUDY_TOTAL 개가 되게 채운다 — "3+7" 을 박지 않는다(복습이 들어오면
        # 본편이 4~5개가 되고 예비는 그만큼 줄어든다). 청크가 모자라면 그냥 짧아진다(R5).
        want = max(0, SURVIVAL_STUDY_TOTAL - len(out))
        out += [
            {"slot": "reserve", "study_kind": "chunk", "item": i}
            for i in survival_reserve[:want]
        ]

    # ⭐ 2026-08-16: **신규가 마르면 남은 칸을 복습으로 메운다.**
    # ⛔ 순서를 지켜라 — ①신규 먼저(배울 게 있으면 배우는 게 우선) ②모자란 만큼 복습
    #   ③그래도 모자라면 짧아진다(진짜 신규 회원 — 정상). 반대로 하면 새 걸 안 배우고
    #   옛것만 돈다. 그래서 이 채움은 **위의 신규 선별이 전부 끝난 뒤**에만 돈다.
    # ⚠ 앞쪽 복습 슬롯(review_slots — 밴드 상한 0~2)과 **별개다**. 그건 "지난 통화분을
    #   먼저 짚는" 설계고 이건 "빈 칸 메우기"다. 한 변수로 합치지 마라.
    # ⚠ 왜 필요한가: L1 은 청크가 46개뿐이라 두어 통화면 대부분 practicing 으로 넘어가고
    #   그때 재료가 1~3개로 떨어진다. 아침에 고친 "가짜 mastered 로 갇힘"과 **결과가 같다**
    #   (그땐 안 배웠는데 재료 0, 이번엔 다 배워서 재료 0).
    # ⛔ **다음 레벨 항목을 당겨오지 마라**(사장님 제안 → 검토 후 반대·동의받음). 두 가지 이유:
    #   ① L1 통화에 L2 문법이 하나라도 섞이면 persona_prompt 의 왕초보 판별
    #      (`is_l1 = "grammar" not in kinds and "chunk" in kinds`)이 깨져 **왕초보 변형
    #      문구가 통째로 사라진다** — 생존회화 학습자에게 문법 용어를 쓰기 시작한다.
    #   ② 복습이 30을 못 채울 만큼 mastered 가 많으면 그 회원은 **이미 승급 조건(G2)에
    #      닿아 있다** — 다음 레벨은 당겨오는 게 아니라 승급으로 가는 것이다.
    target = SURVIVAL_STUDY_TOTAL if band == "survival" else (
        STUDY_MAIN_TOTAL + STUDY_RESERVE_TOTAL
    )
    if len(out) < target:
        filler = _pick_review_items(
            db, member_id, level_no,
            limit=target - len(out), prefer_previous=False, language=language,
            exclude_ids={e["item"].item_id for e in out},
        )
        out += [{"slot": "reserve", "study_kind": i.kind, "item": i} for i in filler]

    _annotate_state(db, member_id, out, chain_call_id=chain_call_id)
    return out


# 프롬프트에 실리는 **학습 상태** 축(유형 축과 별개). DB status → 이 세 값으로 접는다.
STUDY_STATE_NEW = "new"          # 행 없음 — 진짜 처음이다
STUDY_STATE_AGAIN = "again"      # introduced — 한 번 들었다(못 할 확률이 높다)
STUDY_STATE_REVIEW = "review"    # practicing / 미확정 fast-track — 말해본 적 있다


def _annotate_state(db: Session, member_id: int, picked: list[dict],
                    *, chain_call_id: int | None = None) -> None:
    """항목마다 **학습 상태**를 붙인다(2026-08-17 사장님 지시). 제자리 수정.

    🧒 무엇이 문제였나: 라벨 한 칸에 **유형과 상태가 섞여** 있었다
      (`review`=상태 / `grammar·vocab·chunk`=유형). 그래서 introduced(한 번 들은 것)를
      놓을 자리가 없어 **'신규'로 흘렸고**, 프롬프트에서 라벨이 절차를 고르고 절차가
      등급을 정한다:
        신규 절차 = "들려주고 2번 따라 말하게" → 정의상 **E1(모방)**
        복습 절차 = "먼저 물어봐라"            → 답하면 **E2(유도)**
      마스터 조건 ③은 E2/E3 산출이 필요하고 E1 은 안 센다.
      ⇒ 실측(member 20, ko L1): 청크 46개 중 UNSEEN 0 / INTRODUCED 23 / PRACTICING 22 /
        MASTERED 1. 미학습이 0개인데 계속 '신규'로 나갔다(call 1053 증거: E1 7·E3 1·**E2 0**).
      ⭐ 비버는 지시대로 했다. 잘못은 **라벨**이다.
    ⛔ 그래서 축을 둘로 갈랐다 — study_kind 는 이제 **항상 유형**(item.kind)이고,
      상태는 이 함수가 붙이는 state 다. 프롬프트는 "통문장·다시" 처럼 둘을 함께 읽는다.
    ⚠ status=mastered 인데 목록에 있는 경우가 **실제로 있다** — 미확정 fast-track 이다
      (어제 유령 수정으로 복습 풀에 넣었다). 검증이 목적이므로 반드시 물어봐야 해서
      **복습**으로 본다.
    ⚠ slot 은 안 건드린다. 쿼리는 1회(get_progress_map)뿐이다.
    """
    if not picked:
        return
    rows = get_progress_map(db, member_id, [e["item"].item_id for e in picked])
    # ⭐⭐ **이 통화에서 다뤘나**(2026-08-19 사장님 지시). 상태 라벨(새로/다시/복습)은
    #   **평생 상태**라 "방금 다룬 것"과 "3주 전에 배운 것"이 똑같이 `복습` 으로 나간다.
    #   그러면 5개 단위 확인(_STUDY_FIVE_CHECK)이 **방금 다룬 5개를 못 고른다** — 이어하기로
    #   조각이 나뉘면 비버는 앞 조각에서 뭘 했는지 알 방법이 없다.
    #   ⛔ 사장님 지시: "이전에 배웠던 걸 이야기했더라도 복습 시간에는 다뤄야 해."
    #     ⇒ 기준은 **상태가 아니라 이 통화에서 손댔는지**다.
    #
    # ⛔⛔ **범위는 '오늘'이 아니라 '이 통화'다**(사장님 정정): "하루에 두 번 통화를 하면
    #   두 개는 다르게 취급해야지. 하루가 아니라 이번 연달아 통화하는 것들 중에서만이야."
    #   ⇒ 조각들은 **같은 call_id 를 공유**하므로 `prog.last_call_id == 이 체인의 call_id`
    #     하나로 정확히 표현된다. 날짜 비교도, 타임존도 필요 없다(그래서 옛 UTC 경계 문제도
    #     같이 사라졌다). 추가 쿼리도 없다 — 이미 읽은 progress 행에 들어 있다.
    #   ⚠ 조각1(새 통화)에서는 chain_call_id 가 None 이라 아무것도 안 붙는다. 맞다 —
    #     그 시점엔 이 통화에서 다룬 게 없다.
    for e in picked:
        prog = rows.get(e["item"].item_id)
        status = getattr(prog, "status", None) if prog is not None else None
        if prog is None:
            e["state"] = STUDY_STATE_NEW
        elif status == "introduced":
            e["state"] = STUDY_STATE_AGAIN
        else:
            # practicing · mastered(미확정 fast-track) — 둘 다 "말해본 적 있다".
            e["state"] = STUDY_STATE_REVIEW
        if chain_call_id is not None and prog is not None:
            e["this_call"] = getattr(prog, "last_call_id", None) == chain_call_id


def pick_chat_targets(
    db: Session, member_id: int, level_no: int, language: str = "ko"
) -> list[LearningItem]:
    """대화 모드 유도 표현 ≤5 를 선별한다(mechanics ③ — 순수 SELECT).

    3개 = practicing 중 last_used_at 오래된 순(주력 복습) / 1개 = mastered 최고령
    (리텐션 불시 점검) / 1개 = 최근 7일 introduced(갓 배운 것 굳히기). 부족분은
    practicing 으로 보충. 전부 level_no<=내레벨 한정. (멀티랭귀지) language 필터.

    ⚠ **placement 는 뺀다** — 이유는 known_grammar 와 같다. 레벨 배정으로 찍힌 항목을
    "복습·리텐션 점검" 대상으로 삼으면, 해본 적 없는 표현을 다시 꺼내라고 유도하게 된다.
    """
    def base(status_cond):
        return (
            select(LearningItem)
            .join(MemberItemProgress, MemberItemProgress.item_id == LearningItem.item_id)
            .where(
                MemberItemProgress.member_id == member_id,
                MemberItemProgress.provenance != "placement",
                LearningItem.language == language,
                LearningItem.level_no <= level_no,
                status_cond,
            )
        )

    practicing = list(
        db.scalars(
            base(MemberItemProgress.status == "practicing")
            .order_by(
                MemberItemProgress.last_used_at.asc().nulls_first(),
                MemberItemProgress.progress_id.asc(),
            )
            .limit(CHAT_TARGET_TOTAL)  # 보충분까지 한 번에 fetch
        ).all()
    )
    mastered = db.scalars(
        base(MemberItemProgress.status == "mastered")
        .order_by(
            MemberItemProgress.mastered_at.asc().nulls_first(),
            MemberItemProgress.progress_id.asc(),
        )
        .limit(1)
    ).first()
    recent = datetime.now(timezone.utc) - timedelta(days=CHAT_INTRODUCED_RECENT_DAYS)
    introduced = db.scalars(
        base(
            and_(
                MemberItemProgress.status == "introduced",
                MemberItemProgress.first_seen_at >= recent,
            )
        )
        .order_by(
            MemberItemProgress.first_seen_at.desc(), MemberItemProgress.progress_id.desc()
        )
        .limit(1)
    ).first()

    out: list[LearningItem] = []
    seen: set[int] = set()

    def add(item: Optional[LearningItem]) -> None:
        if item is not None and item.item_id not in seen and len(out) < CHAT_TARGET_TOTAL:
            seen.add(item.item_id)
            out.append(item)

    for i in practicing[:CHAT_PRACTICING_SLOTS]:
        add(i)
    add(mastered)
    add(introduced)
    for i in practicing[CHAT_PRACTICING_SLOTS:]:  # 부족분 practicing 보충
        add(i)
    return out


def known_grammar(db: Session, member_id: int, language: str = "ko") -> list[str]:
    """아는 문법 표기 ≤40(mechanics ③ soft 범위) — practicing/mastered, 최신 레벨 우선.

    (멀티랭귀지) learning_item.language 로 대상 언어 문법만.

    ⚠ **placement 는 뺀다.** 레벨 k 를 배정받으면 하위 레벨 항목이 grandfathering 으로
    한꺼번에 introduced/mastered 로 찍히는데(apply_grandfathering), 그건 "재교육하지
    마라"는 **선별용 표시**지 학습자가 실제로 해본 것이 아니다. 이걸 프롬프트에 넣으면
    비버가 배운 적 없는 표현을 두고 "그거 기억나?" 라고 한다(실측: 일본어 2단계 배정
    직후 1단계 46개가 증거 0건인 채 introduced → 비버가 자기소개를 배웠다고 말함).

    실증거가 붙으면 provenance 가 placement → observed 로 승격되므로(mastery_service),
    이 조건은 정확히 "아직 증거가 없는 것" 만 걸러낸다. 재교육 방지는 pick_study_items
    가 progress 행 존재로 판단하니 그대로 유지된다.
    """
    rows = db.scalars(
        select(LearningItem.surface)
        .join(MemberItemProgress, MemberItemProgress.item_id == LearningItem.item_id)
        .where(
            MemberItemProgress.member_id == member_id,
            MemberItemProgress.status.in_(("practicing", "mastered")),
            MemberItemProgress.provenance != "placement",
            LearningItem.language == language,
            LearningItem.kind == "grammar",
        )
        .order_by(
            LearningItem.level_no.desc(),
            LearningItem.seq_no.desc().nulls_last(),
            LearningItem.item_id.desc(),
        )
        .limit(KNOWN_GRAMMAR_CAP)
    ).all()
    return list(dict.fromkeys(s for s in rows if s))


def bridge_or_struggle_ratio(
    db: Session, member_id: int, language: str = "ko"
) -> float:
    """복습 비중을 파생 계산한다(mechanics ⑨ — 별도 필드 없음, 통화당 1회).

    1) 레벨 진입(history 최신 행, 없으면 가입) 후 증거통화 <3 → 0.7(무조건 브리지)
    2) 진입 5통화 이내 && 최근 3 증거통화가 전부 현재 레벨 버벅임
       (F비율>=0.5 || E2+ 성공 0) → 0.7(소프트 강등 믹스)
    3) 그 외 → 0.3(정상 믹스). 회복(F<0.3 통화 2연속)은 2의 "3연속" 파탄으로 자연 충족.
    (멀티랭귀지) 레벨·history·증거통화·버벅임 집계를 전부 language 로 스코프.
    """
    member = db.get(Member, member_id)
    if member is None:
        return NORMAL_REVIEW_RATIO
    k = get_language_level(db, member_id, language)
    if k is None:
        return NORMAL_REVIEW_RATIO
    latest = get_latest_history(db, member_id, language)
    entry_at = latest.created_at if latest is not None else member.created_at
    evidence_calls = count_evidence_calls_since(db, member_id, entry_at, language)
    if evidence_calls < _BRIDGE_ENTRY_EVIDENCE_CALLS:
        return BRIDGE_REVIEW_RATIO
    if evidence_calls <= _STRUGGLE_ENTRY_EVIDENCE_CALLS:
        recent_ids = list_recent_evidence_call_ids(
            db, member_id, limit=_STRUGGLE_WINDOW, language=language
        )
        if len(recent_ids) == _STRUGGLE_WINDOW:
            per_call: dict[int, dict[str, int]] = {cid: {} for cid in recent_ids}
            rows = db.execute(
                select(ItemEvidence.call_id, ItemEvidence.grade_final, func.count())
                .join(LearningItem, LearningItem.item_id == ItemEvidence.item_id)
                .where(
                    ItemEvidence.member_id == member_id,
                    ItemEvidence.language == language,
                    ItemEvidence.call_id.in_(recent_ids),
                    LearningItem.level_no == k,
                )
                .group_by(ItemEvidence.call_id, ItemEvidence.grade_final)
            ).all()
            for cid, grade, cnt in rows:
                per_call[cid][grade] = cnt

            def _struggles(grades: dict[str, int]) -> bool:
                total = sum(grades.values())
                f_ratio = (grades.get("F", 0) / total) if total else 0.0
                e2plus = grades.get("E2", 0) + grades.get("E3", 0)
                return f_ratio >= _STRUGGLE_F_RATIO or e2plus == 0

            if all(_struggles(per_call[cid]) for cid in recent_ids):
                return BRIDGE_REVIEW_RATIO
    return NORMAL_REVIEW_RATIO


def promotion_pending(db: Session, member_id: int, language: str = "ko") -> bool:
    """승급 멘트 여부(mechanics ⑧) — 최신 history 가 gate_promotion && 이후 증거통화 0.

    (멀티랭귀지) history·증거통화 집계를 language 로 스코프.
    """
    latest = get_latest_history(db, member_id, language)
    if latest is None or latest.reason != "gate_promotion":
        return False
    return count_evidence_calls_since(db, member_id, latest.created_at, language) == 0


# --------------------------------------------------------------------------- #
# 진행/증거 조회 (⑥ 상태 전이 재료)
# --------------------------------------------------------------------------- #
def get_progress_map(
    db: Session, member_id: int, item_ids: Sequence[int]
) -> dict[int, MemberItemProgress]:
    """회원×항목 progress 행을 item_id 로 색인해 반환한다(행 부재=UNSEEN)."""
    if not item_ids:
        return {}
    rows = db.scalars(
        select(MemberItemProgress).where(
            MemberItemProgress.member_id == member_id,
            MemberItemProgress.item_id.in_(list(item_ids)),
        )
    ).all()
    return {r.item_id: r for r in rows}


def list_evidence_for_items(
    db: Session, member_id: int, item_ids: Sequence[int]
) -> list[ItemEvidence]:
    """회원×항목 증거 시계열(과거 통화분) — 상태 전이 판정(2통화·2일 분산 등)의 원본."""
    if not item_ids:
        return []
    return list(
        db.scalars(
            select(ItemEvidence)
            .where(
                ItemEvidence.member_id == member_id,
                ItemEvidence.item_id.in_(list(item_ids)),
            )
            .order_by(ItemEvidence.created_at.asc(), ItemEvidence.evidence_id.asc())
        ).all()
    )


def list_unconfirmed_fast_track(
    db: Session, member_id: int, language: str = "ko"
) -> Sequence[MemberItemProgress]:
    """fast-track 미확정(mastered && provenance=fast_track && confirmed_at NULL) 행 전부.

    (멀티랭귀지) member_item_progress 엔 language 컬럼이 없어 learning_item 조인으로
    대상 언어만 필터한다(member-only 오염 차단 — 다른 언어 fast-track 미확정 제외).
    """
    return db.scalars(
        select(MemberItemProgress)
        .join(LearningItem, LearningItem.item_id == MemberItemProgress.item_id)
        .where(
            MemberItemProgress.member_id == member_id,
            MemberItemProgress.status == "mastered",
            MemberItemProgress.provenance == "fast_track",
            MemberItemProgress.fast_track_confirmed_at.is_(None),
            LearningItem.language == language,
        )
    ).all()


def has_f_evidence_since(
    db: Session, member_id: int, item_id: int, since: Optional[datetime]
) -> bool:
    """해당 항목에 since 이후 F 증거가 있는가 — fast-track 14일 무F 자동 확정 판정."""
    stmt = select(ItemEvidence.evidence_id).where(
        ItemEvidence.member_id == member_id,
        ItemEvidence.item_id == item_id,
        ItemEvidence.grade_final == "F",
    )
    if since is not None:
        stmt = stmt.where(ItemEvidence.created_at >= since)
    return db.scalar(stmt.limit(1)) is not None


# --------------------------------------------------------------------------- #
# 레벨업 게이트 집계 (⑦)
# --------------------------------------------------------------------------- #
def list_gate_items(
    db: Session, level_no: int, language: str = "ko"
) -> list[tuple[int, str]]:
    """레벨 k 의 게이트 대상 항목 [(item_id, kind)] — G1/G2 분모.

    승급은 문법(+L1 청크) 전용 — D12(마스터 플랜 결정 로그): 어휘는 게이트 미산입
    (추적·복습·grandfathering·is_core 우선순위는 전부 유지, 판정만 제외).
    grammar 는 단원 순서(seq_no) 상위 min(수, 45)만(초과분 '선택 문법' 제외 — 기존 상한),
    chunk 는 전부(L1 특례 — L1 은 문법 0이라 청크가 게이트 기준).
    (멀티랭귀지) learning_item.language 로 대상 언어 게이트만.
    """
    grammar_ids = db.scalars(
        select(LearningItem.item_id)
        .where(
            LearningItem.language == language,
            LearningItem.level_no == level_no,
            LearningItem.kind == "grammar",
        )
        .order_by(LearningItem.seq_no.asc().nulls_last(), LearningItem.item_id.asc())
        .limit(GRAMMAR_GATE_CAP)
    ).all()
    chunk_ids = db.scalars(
        select(LearningItem.item_id).where(
            LearningItem.language == language,
            LearningItem.level_no == level_no,
            LearningItem.kind == "chunk",
        )
    ).all()
    return [(i, "grammar") for i in grammar_ids] + [(i, "chunk") for i in chunk_ids]


def get_member_for_update(db: Session, member_id: int) -> Optional[Member]:
    """member 행 FOR UPDATE — 동시 분석 경합 방지(sqlite 에선 no-op)."""
    return db.scalar(
        select(Member).where(Member.member_id == member_id).with_for_update()
    )


def has_call_evidence(db: Session, call_id: int, since_turn_index: int | None = None) -> bool:
    """이 통화의 증거가 이미 적립됐는지 — 분석 재실행 이중 적립 방지(리뷰 M4).

    ⭐⭐ **`since_turn_index` 는 이어하기 때문에 생겼다**(2026-08-19 실측 사고).
      조각들이 **같은 call_id 를 공유**하므로(이어하기 설계), 조각1이 증거를 남기면
      call_id 기준 가드가 조각2를 **통째로 스킵**한다:
          WARNING 체크판: call_id=1093 증거 기존재 → 스킵(이중 적립 방지)
          체크판: 검출 3→검증 0, 증거 None
      사장님이 조각2에서 정확히 말했는데 진도가 하나도 안 쌓였다.

    ⇒ 이어하기 조각은 **그 조각의 턴 범위**로 묻는다: "turn_index >= N 에 이미 증거가 있나".
      같은 조각을 두 번 분석하면 여전히 막히고(원래 목적 유지), 다음 조각은 통과한다.
    ⚠ `turn_index` 가 NULL 인 옛 증거는 범위 판정에서 빠진다 — 그 행들은 이어하기 이전
      통화의 것이라 조각 경계와 무관하다(막을 이유가 없다).
    """
    q = select(ItemEvidence.evidence_id).where(ItemEvidence.call_id == call_id)
    if since_turn_index:
        q = q.where(ItemEvidence.turn_index >= since_turn_index)
    return db.scalar(q.limit(1)) is not None


def get_history_by_trigger(db: Session, trigger_call_id: int) -> Optional[MemberLevelHistory]:
    """trigger_call_id 로 history 조회 — evaluate_level_up 멱등 키."""
    return db.scalar(
        select(MemberLevelHistory).where(
            MemberLevelHistory.trigger_call_id == trigger_call_id
        )
    )


def get_latest_history(
    db: Session, member_id: int, language: str = "ko"
) -> Optional[MemberLevelHistory]:
    """회원×언어 최신 레벨 이력 — created_at 이 "현재 레벨 진입 시각"의 단일 소스.

    ⚠(멀티랭귀지) member-only 집계의 진입시각 원천 — language 필터가 빠지면 ja 통화가
    ko 진입시각을 오염시킨다(리스크 1, 치명). member_level_history.language 로 스코프.
    """
    return db.scalar(
        select(MemberLevelHistory)
        .where(
            MemberLevelHistory.member_id == member_id,
            MemberLevelHistory.language == language,
        )
        .order_by(MemberLevelHistory.created_at.desc(), MemberLevelHistory.history_id.desc())
        .limit(1)
    )


def count_evidence_calls_since(
    db: Session, member_id: int, since: Optional[datetime], language: str = "ko"
) -> int:
    """since 이후 증거통화 수 — item_evidence 에 행이 있는 distinct call (D15 파생).

    브리지(진입 후 <3)·promotion_pending(승급 후 0) 판정 재료. call 컬럼 캐시 없이
    증거 로그에서 직접 계산한다(관통 원칙 2). ix_evidence_member_lang_call 로 커버.
    (멀티랭귀지) member-only 집계 — item_evidence.language 필터(오염 차단).
    """
    stmt = select(func.count(func.distinct(ItemEvidence.call_id))).where(
        ItemEvidence.member_id == member_id,
        ItemEvidence.language == language,
    )
    if since is not None:
        stmt = stmt.where(ItemEvidence.created_at > since)
    return int(db.scalar(stmt) or 0)


def list_recent_evidence_call_ids(
    db: Session, member_id: int, limit: int = 5, language: str = "ko"
) -> list[int]:
    """최근 증거통화 call_id 목록(최신순) — G4(F비율)·버벅임 감지의 창(D15).

    call_id 는 단조 증가라 call_id DESC 가 곧 최신순(같은 흐름의 _sel3_cooldown_item_ids
    와 동일 기준 — 통화 시각 컬럼 조인 불요).
    (멀티랭귀지) member-only 집계 — item_evidence.language 필터(오염 차단).
    """
    return list(
        db.scalars(
            select(ItemEvidence.call_id)
            .where(
                ItemEvidence.member_id == member_id,
                ItemEvidence.language == language,
            )
            .group_by(ItemEvidence.call_id)
            .order_by(ItemEvidence.call_id.desc())
            .limit(limit)
        ).all()
    )


def evidence_grade_counts(
    db: Session, member_id: int, call_ids: Sequence[int], level_no: int,
    language: str = "ko",
) -> tuple[int, int]:
    """지정 통화들의 (F 증거 수, 전체 증거 수) — 현재 레벨 항목 한정(G4).

    (멀티랭귀지) item_evidence.language 로 대상 언어 증거만(오염 차단).
    """
    if not call_ids:
        return (0, 0)
    rows = db.execute(
        select(ItemEvidence.grade_final, func.count())
        .join(LearningItem, LearningItem.item_id == ItemEvidence.item_id)
        .where(
            ItemEvidence.member_id == member_id,
            ItemEvidence.language == language,
            ItemEvidence.call_id.in_(list(call_ids)),
            LearningItem.level_no == level_no,
        )
        .group_by(ItemEvidence.grade_final)
    ).all()
    total = sum(c for _, c in rows)
    f_count = sum(c for g, c in rows if g == "F")
    return (f_count, total)


# --------------------------------------------------------------------------- #
# grandfathering (⑩ — 레벨테스트 배정 직후 체크판 초기화)
# --------------------------------------------------------------------------- #
def existing_progress_item_ids(db: Session, member_id: int) -> set[int]:
    """회원이 이미 progress 행을 가진 item_id 집합 — placement 는 기존 행을 덮지 않음."""
    return set(
        db.scalars(
            select(MemberItemProgress.item_id).where(
                MemberItemProgress.member_id == member_id
            )
        ).all()
    )


def demote_mastered_to_introduced(
    db: Session, member_id: int, *, max_level: int, language: str = "ko"
) -> int:
    """레벨이 **내려갔을 때** 그 레벨 이하의 `mastered` 를 `introduced` 로 되돌린다.

    ⭐ 사장님 설계(2026-08-16): *"레벨1부터 해서 레벨3까지 올라갔으면 레벨1 기록은 다 있겠지?
      근데 여기서 레벨1로 내려가면 레벨1·2는 **배운 흔적은 있는데 마스터는 안 된 걸로** 처리하는
      거지. **한 번 배운다고 마스터 처리하는 건 아니지 않아?** 그렇게 해서 복습만 하는 형태로."*
    ⇒ 행을 **지우지 않는다**(배운 흔적은 남는다). 상태와 점수만 되돌려 복습 대상으로 돌린다.

    ⛔ `item_evidence` 는 **건드리지 않는다** — append-only 감사 로그다(CLAUDE.md).
      증거는 "그때 실제로 이렇게 말했다"는 사실이고, 강등은 그 사실을 지우는 게 아니다.
    ⚠ `mastered_at` 도 비운다 — 상태가 mastered 가 아닌데 그 시각이 남아 있으면 거짓말이다.
    ⚠ 되돌리는 것은 **mastered 뿐**이다. practicing/introduced 는 이미 복습 대상이라 그대로 둔다.

    Returns: 되돌린 행 수.
    """
    item_ids = select(LearningItem.item_id).where(
        LearningItem.language == language,
        LearningItem.level_no <= max_level,
    )
    rows = list(db.scalars(
        select(MemberItemProgress).where(
            MemberItemProgress.member_id == member_id,
            MemberItemProgress.status == "mastered",
            MemberItemProgress.item_id.in_(item_ids),
        )
    ).all())
    for prog in rows:
        prog.status = "introduced"
        prog.score = 0.0
        prog.mastered_at = None
    return len(rows)


def list_grandfather_item_ids(
    db: Session, *, max_level: Optional[int] = None, exact_level: Optional[int] = None,
    language: str = "ko",
) -> list[int]:
    """grandfathering 대상 item_id — grammar 전체 + chunk + core 어휘(is_core).

    D12 이후에도 core 어휘를 계속 포함한다(게이트 판정이 아니라 복습·재교육 방지 목적 —
    무변경). non-core 어휘(노출 풀)는 제외: 행 부재=UNSEEN 이 기본이므로 레벨당 수천 행
    insert(placement 행 폭발)를 피한다. grammar 는 45 초과 '선택 문법'도 포함 —
    재교육 방지(대화 재활용 풀 잔류) 목적. (멀티랭귀지) learning_item.language 필터.
    """
    stmt = select(LearningItem.item_id).where(
        LearningItem.language == language,
        or_(
            LearningItem.kind.in_(("grammar", "chunk")),
            and_(LearningItem.kind == "vocab", LearningItem.is_core.is_(True)),
        ),
    )
    if max_level is not None:
        stmt = stmt.where(LearningItem.level_no <= max_level)
    if exact_level is not None:
        stmt = stmt.where(LearningItem.level_no == exact_level)
    return list(db.scalars(stmt).all())
