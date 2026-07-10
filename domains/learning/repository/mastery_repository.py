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
) -> list[dict]:
    """검출 후보 기본 구성 — practicing 오래된 순 18 + introduced 최신 12 (상한 30).

    c2(프롬프트 주입) 전이므로 "오늘 주입 ~12" 자리를 introduced 최신분으로 채운다.
    c2 가 주입 항목을 analyze_call 의 candidates 인자로 넘기기 시작하면 이 함수는
    폴백(미전달 시)으로만 쓰인다. injected 는 전부 False(미주입 취급 — fast-track ④).
    """
    # 주력: practicing 을 last_used_at 오래된 순(미사용 NULL 최우선)으로 — 가장 위태로운 순.
    practicing = db.scalars(
        select(LearningItem)
        .join(MemberItemProgress, MemberItemProgress.item_id == LearningItem.item_id)
        .where(
            MemberItemProgress.member_id == member_id,
            MemberItemProgress.status == "practicing",
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
# 통화 시작 선별 (① 3-b~e — 공부 10 / 대화 유도 5 / 아는 문법 / 브리지 / 승급 멘트)
# --------------------------------------------------------------------------- #
# 공부 로드(mechanics ② — 본편 5 + 예비 5 = 10, D4) + 밴드별 본편 구성 상한.
STUDY_MAIN_TOTAL = 5
STUDY_RESERVE_TOTAL = 5
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


def band_of(level_no: int) -> str:
    """레벨(1~13)→밴드 — mastery_service._band_of 와 동일 경계.

    (repository→service 역임포트가 순환이라 여기 중복 정의 — 경계 변경 시 양쪽 동시 수정.)
    """
    if level_no <= 1:
        return "survival"
    if level_no <= 5:
        return "beginner"
    if level_no <= 9:
        return "intermediate"
    return "advanced"


def _sel3_cooldown_item_ids(db: Session, member_id: int) -> set[int]:
    """SEL3 쿨다운 대상 — 최근 2통화(증거 기준)에서 성공(E1+)했고 F 없는 항목.

    F 가 있으면 "즉시 재출제"라 제외하지 않는다(성공-F 차집합).
    """
    recent_calls = list(
        db.scalars(
            select(ItemEvidence.call_id)
            .where(ItemEvidence.member_id == member_id)
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
    db: Session, member_id: int, level_no: int, *, limit: int, prefer_previous: bool
) -> list[LearningItem]:
    """복습 선별 — practicing && level_no<=내레벨, last_used_at 오래된 순.

    감쇠점수 정렬은 P3 — P0 정책은 "감쇠 없이 오래된 순". 브리지/버벅임
    (prefer_previous=True)이면 이전 레벨(level_no<k) 항목을 먼저 채운다(⑨ 비중 확대).
    """
    if limit <= 0:
        return []

    def fetch(level_cond, n: int) -> list[LearningItem]:
        if n <= 0:
            return []
        return list(
            db.scalars(
                select(LearningItem)
                .join(
                    MemberItemProgress,
                    MemberItemProgress.item_id == LearningItem.item_id,
                )
                .where(
                    MemberItemProgress.member_id == member_id,
                    MemberItemProgress.status == "practicing",
                    level_cond,
                )
                .order_by(
                    MemberItemProgress.last_used_at.asc().nulls_first(),
                    MemberItemProgress.progress_id.asc(),
                )
                .limit(n)
            ).all()
        )

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
) -> list[dict]:
    """공부 모드 로드 10(본편 5+예비 5)을 선별한다(mechanics ② — 순수 SELECT).

    review_slots(복습 슬롯 수)는 호출부가 밴드·브리지(⑨)로 결정해 넘긴다 —
    브리지/버벅임 시 확대. bridge_prev_ratio>=0.5 면 복습을 이전 레벨 항목부터 채운다.

    밴드별 본편 구성: L1 = 청크 3+어휘 1(문법 0) / 초급 = 복습 0~2+문법 1+어휘 2~4 /
    중급 = 어휘 상한 3 / 고급 = 복습 0~1+문법 1+어휘 3~4. 예비 5 는 전부 어휘(다음 순번).
    제외 필터: SEL1(MASTERED — 신규 풀이 미학습 한정이라 자연 충족) / SEL2 / SEL3.

    Returns:
        [{"slot": "main"|"reserve", "study_kind": "review"|"grammar"|"vocab"|"chunk",
          "item": LearningItem}] — 본편(복습→[L1 청크|문법]→어휘)→예비 순서. 시드/진행
        데이터가 부족하면 짧아지거나 빈 리스트(호출부가 None 처리 — R5).
    """
    band = band_of(level_no)
    cooldown = _sel3_cooldown_item_ids(db, member_id)

    reviews = _pick_review_items(
        db, member_id, level_no,
        limit=review_slots, prefer_previous=bridge_prev_ratio >= 0.5,
    )
    out: list[dict] = [{"slot": "main", "study_kind": "review", "item": i} for i in reviews]

    if band == "survival":
        chunks = _pick_new_items(
            db, member_id, level_no, "chunk", _SURVIVAL_CHUNKS, exclude_ids=cooldown
        )
        out += [{"slot": "main", "study_kind": "chunk", "item": i} for i in chunks]
    else:
        # 신규 문법 정확히 1개(2개 금지 — 항목당 75~90초).
        grammars = _pick_new_items(
            db, member_id, level_no, "grammar", 1, exclude_ids=cooldown
        )
        out += [{"slot": "main", "study_kind": "grammar", "item": i} for i in grammars]

    vocab_want = STUDY_MAIN_TOTAL - len(out)
    if band == "survival":
        vocab_want = min(vocab_want, _SURVIVAL_VOCAB_CAP)
    elif band == "intermediate":
        vocab_want = min(vocab_want, _INTERMEDIATE_VOCAB_CAP)
    vocab_want = max(0, vocab_want)

    vocabs = _pick_new_items(
        db, member_id, level_no, "vocab",
        vocab_want + STUDY_RESERVE_TOTAL, exclude_ids=cooldown,
    )
    out += [{"slot": "main", "study_kind": "vocab", "item": i} for i in vocabs[:vocab_want]]
    out += [
        {"slot": "reserve", "study_kind": "vocab", "item": i}
        for i in vocabs[vocab_want : vocab_want + STUDY_RESERVE_TOTAL]
    ]
    return out


def pick_chat_targets(db: Session, member_id: int, level_no: int) -> list[LearningItem]:
    """대화 모드 유도 표현 ≤5 를 선별한다(mechanics ③ — 순수 SELECT).

    3개 = practicing 중 last_used_at 오래된 순(주력 복습) / 1개 = mastered 최고령
    (리텐션 불시 점검) / 1개 = 최근 7일 introduced(갓 배운 것 굳히기). 부족분은
    practicing 으로 보충. 전부 level_no<=내레벨 한정.
    """
    def base(status_cond):
        return (
            select(LearningItem)
            .join(MemberItemProgress, MemberItemProgress.item_id == LearningItem.item_id)
            .where(
                MemberItemProgress.member_id == member_id,
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


def known_grammar(db: Session, member_id: int) -> list[str]:
    """아는 문법 표기 ≤40(mechanics ③ soft 범위) — practicing/mastered, 최신 레벨 우선."""
    rows = db.scalars(
        select(LearningItem.surface)
        .join(MemberItemProgress, MemberItemProgress.item_id == LearningItem.item_id)
        .where(
            MemberItemProgress.member_id == member_id,
            MemberItemProgress.status.in_(("practicing", "mastered")),
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


def bridge_or_struggle_ratio(db: Session, member_id: int) -> float:
    """복습 비중을 파생 계산한다(mechanics ⑨ — 별도 필드 없음, 통화당 1회).

    1) 레벨 진입(history 최신 행, 없으면 가입) 후 증거통화 <3 → 0.7(무조건 브리지)
    2) 진입 5통화 이내 && 최근 3 증거통화가 전부 현재 레벨 버벅임
       (F비율>=0.5 || E2+ 성공 0) → 0.7(소프트 강등 믹스)
    3) 그 외 → 0.3(정상 믹스). 회복(F<0.3 통화 2연속)은 2의 "3연속" 파탄으로 자연 충족.
    """
    member = db.get(Member, member_id)
    if member is None or member.korean_level is None:
        return NORMAL_REVIEW_RATIO
    k = member.korean_level
    latest = get_latest_history(db, member_id)
    entry_at = latest.created_at if latest is not None else member.created_at
    evidence_calls = count_evidence_calls_since(db, member_id, entry_at)
    if evidence_calls < _BRIDGE_ENTRY_EVIDENCE_CALLS:
        return BRIDGE_REVIEW_RATIO
    if evidence_calls <= _STRUGGLE_ENTRY_EVIDENCE_CALLS:
        recent_ids = list_recent_evidence_call_ids(db, member_id, limit=_STRUGGLE_WINDOW)
        if len(recent_ids) == _STRUGGLE_WINDOW:
            per_call: dict[int, dict[str, int]] = {cid: {} for cid in recent_ids}
            rows = db.execute(
                select(ItemEvidence.call_id, ItemEvidence.grade_final, func.count())
                .join(LearningItem, LearningItem.item_id == ItemEvidence.item_id)
                .where(
                    ItemEvidence.member_id == member_id,
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


def promotion_pending(db: Session, member_id: int) -> bool:
    """승급 멘트 여부(mechanics ⑧) — 최신 history 가 gate_promotion && 이후 증거통화 0."""
    latest = get_latest_history(db, member_id)
    if latest is None or latest.reason != "gate_promotion":
        return False
    return count_evidence_calls_since(db, member_id, latest.created_at) == 0


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


def list_unconfirmed_fast_track(db: Session, member_id: int) -> Sequence[MemberItemProgress]:
    """fast-track 미확정(mastered && provenance=fast_track && confirmed_at NULL) 행 전부."""
    return db.scalars(
        select(MemberItemProgress).where(
            MemberItemProgress.member_id == member_id,
            MemberItemProgress.status == "mastered",
            MemberItemProgress.provenance == "fast_track",
            MemberItemProgress.fast_track_confirmed_at.is_(None),
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
def list_gate_items(db: Session, level_no: int) -> list[tuple[int, str]]:
    """레벨 k 의 게이트 대상 항목 [(item_id, kind)] — G1/G2 분모.

    승급은 문법(+L1 청크) 전용 — D12(마스터 플랜 결정 로그): 어휘는 게이트 미산입
    (추적·복습·grandfathering·is_core 우선순위는 전부 유지, 판정만 제외).
    grammar 는 단원 순서(seq_no) 상위 min(수, 45)만(초과분 '선택 문법' 제외 — 기존 상한),
    chunk 는 전부(L1 특례 — L1 은 문법 0이라 청크가 게이트 기준).
    """
    grammar_ids = db.scalars(
        select(LearningItem.item_id)
        .where(LearningItem.level_no == level_no, LearningItem.kind == "grammar")
        .order_by(LearningItem.seq_no.asc().nulls_last(), LearningItem.item_id.asc())
        .limit(GRAMMAR_GATE_CAP)
    ).all()
    chunk_ids = db.scalars(
        select(LearningItem.item_id).where(
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


def has_call_evidence(db: Session, call_id: int) -> bool:
    """해당 통화의 증거가 이미 적립됐는지 — 분석 재실행 이중 적립 방지(리뷰 M4)."""
    return db.scalar(
        select(ItemEvidence.evidence_id).where(ItemEvidence.call_id == call_id).limit(1)
    ) is not None


def get_history_by_trigger(db: Session, trigger_call_id: int) -> Optional[MemberLevelHistory]:
    """trigger_call_id 로 history 조회 — evaluate_level_up 멱등 키."""
    return db.scalar(
        select(MemberLevelHistory).where(
            MemberLevelHistory.trigger_call_id == trigger_call_id
        )
    )


def get_latest_history(db: Session, member_id: int) -> Optional[MemberLevelHistory]:
    """회원 최신 레벨 이력 — created_at 이 "현재 레벨 진입 시각"의 단일 소스."""
    return db.scalar(
        select(MemberLevelHistory)
        .where(MemberLevelHistory.member_id == member_id)
        .order_by(MemberLevelHistory.created_at.desc(), MemberLevelHistory.history_id.desc())
        .limit(1)
    )


def count_evidence_calls_since(db: Session, member_id: int, since: Optional[datetime]) -> int:
    """since 이후 증거통화 수 — item_evidence 에 행이 있는 distinct call (D15 파생).

    브리지(진입 후 <3)·promotion_pending(승급 후 0) 판정 재료. call 컬럼 캐시 없이
    증거 로그에서 직접 계산한다(관통 원칙 2). ix_evidence_member_item(member_id 선두)로 커버.
    """
    stmt = select(func.count(func.distinct(ItemEvidence.call_id))).where(
        ItemEvidence.member_id == member_id
    )
    if since is not None:
        stmt = stmt.where(ItemEvidence.created_at > since)
    return int(db.scalar(stmt) or 0)


def list_recent_evidence_call_ids(db: Session, member_id: int, limit: int = 5) -> list[int]:
    """최근 증거통화 call_id 목록(최신순) — G4(F비율)·버벅임 감지의 창(D15).

    call_id 는 단조 증가라 call_id DESC 가 곧 최신순(같은 흐름의 _sel3_cooldown_item_ids
    와 동일 기준 — 통화 시각 컬럼 조인 불요).
    """
    return list(
        db.scalars(
            select(ItemEvidence.call_id)
            .where(ItemEvidence.member_id == member_id)
            .group_by(ItemEvidence.call_id)
            .order_by(ItemEvidence.call_id.desc())
            .limit(limit)
        ).all()
    )


def evidence_grade_counts(
    db: Session, member_id: int, call_ids: Sequence[int], level_no: int
) -> tuple[int, int]:
    """지정 통화들의 (F 증거 수, 전체 증거 수) — 현재 레벨 항목 한정(G4)."""
    if not call_ids:
        return (0, 0)
    rows = db.execute(
        select(ItemEvidence.grade_final, func.count())
        .join(LearningItem, LearningItem.item_id == ItemEvidence.item_id)
        .where(
            ItemEvidence.member_id == member_id,
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


def list_grandfather_item_ids(
    db: Session, *, max_level: Optional[int] = None, exact_level: Optional[int] = None
) -> list[int]:
    """grandfathering 대상 item_id — grammar 전체 + chunk + core 어휘(is_core).

    D12 이후에도 core 어휘를 계속 포함한다(게이트 판정이 아니라 복습·재교육 방지 목적 —
    무변경). non-core 어휘(노출 풀)는 제외: 행 부재=UNSEEN 이 기본이므로 레벨당 수천 행
    insert(placement 행 폭발)를 피한다. grammar 는 45 초과 '선택 문법'도 포함 —
    재교육 방지(대화 재활용 풀 잔류) 목적.
    """
    stmt = select(LearningItem.item_id).where(
        or_(
            LearningItem.kind.in_(("grammar", "chunk")),
            and_(LearningItem.kind == "vocab", LearningItem.is_core.is_(True)),
        )
    )
    if max_level is not None:
        stmt = stmt.where(LearningItem.level_no <= max_level)
    if exact_level is not None:
        stmt = stmt.where(LearningItem.level_no == exact_level)
    return list(db.scalars(stmt).all())
