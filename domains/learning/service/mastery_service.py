"""mastery 서비스 — 증거 반영·상태 전이·fast-track·레벨업 판정·grandfathering.

체크판(member_item_progress)의 상태·점수는 전부 item_evidence(append-only)의
파생값이다(관통 원칙 2). 이 모듈은 통화후 분석 파이프라인(mechanics ⑤ 6~7단계)의
"코드가 심판" 부분: 검증된 증거를 받아 점수·카운터·상태를 갱신하고, 통화 분석
커밋 흐름에서 레벨업 게이트를 판정한다.

⚠ commit 규칙: 이 모듈의 함수는 **commit 하지 않는다**. 호출 흐름이 한 번에 커밋한다 —
    - normalcall: normalcall_service._apply_call_mastery (증거→전이→레벨업 1 commit)
    - leveltest : normalcall_service._save_level_assessment (레벨 배정+grandfathering 1 commit)
부분 커밋 창(증거만 반영되고 승급 판정이 누락되는 상태)을 만들지 않기 위함.

승급 게이트는 **문법(+L1 청크) 전용 — D12**(2026-07-09): 어휘는 G1/G2 판정에서 제외한다.
어휘의 추적(증거·점수·상태 전이)·복습 선별·grandfathering·is_core(가르치는 순서
우선순위)는 전부 유지 — 승급 판정에만 산입하지 않는다.

체류 게이트(G3)는 **폐지 — D15**(2026-07-09): "충실한 통화 N회"는 G1(배움)·G2(잘씀)와
이중 규제였다(문법 90% 배움 + 잘씀을 채우면 통화 수는 자연 충족). "유효통화" 개념도
함께 폐지 — 통화 수 파생값이 필요한 곳은 전부 **증거통화**(item_evidence 에 행이 있는
distinct call)로 계산한다(관통 원칙 2 — 컬럼 캐시 불요). G5(승급 후 잠금)는 연쇄 승급
방지 안전핀으로 **일수 조건만** 유지한다.

설계: docs/20260709_1346_level-system-detailed-mechanics.md ⑤~⑦·⑩ + 관통 원칙 3개,
      docs/20260709_1231_level-system-master-plan.md §5 + 결정 D12·D15.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from domains.learning.models.item_evidence import ItemEvidence
from domains.learning.models.member_item_progress import MemberItemProgress
from domains.learning.models.member_level_history import MemberLevelHistory
from domains.learning.repository import mastery_repository

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 파라미터 (mechanics 파라미터 총괄표 — 전부 튜닝 가능, ★ 게이밍 방어는 고정)
# --------------------------------------------------------------------------- #
# 점수 Δ: E0 노출 / E1 모방 / E2 유도 / E3 자발 / F 오류. 클램프 0~6.
SCORE_DELTA: dict[str, float] = {"E0": 0.25, "E1": 0.5, "E2": 1.0, "E3": 1.5, "F": -1.0}
SCORE_MIN, SCORE_MAX = 0.0, 6.0
NET_GAIN_CAP_PER_CALL = 2.0  # 통화당 항목별 순증 상한(★ 문장 반복 게이밍 방어)

MASTERED_MIN_SCORE = 3.0
# D14(사장님 결정): 잘씀 확정에 필요한 성공 산출(E2 유도/E3 자발) 최소 횟수.
# 따라말하기(E1)는 미산입. chunk(L1 생존)는 2회 특례(_mastered_conditions_met 참조).
MASTERED_MIN_PRODUCTIONS = 3
# 통화당 승격 상한: MASTERED 문법 2·어휘 6·청크 4 / 신규 INTRODUCED 8 / fast-track 2
MASTERED_PROMOTION_CAPS: dict[str, int] = {"grammar": 2, "vocab": 6, "chunk": 4}
INTRODUCED_CAP_PER_CALL = 8
FAST_TRACK_CAP_PER_CALL = 2

FAST_TRACK_JACCARD_MAX = 0.5     # E3 2건 "서로 다른 문장": hash 상이 && 어절 Jaccard<0.5
FAST_TRACK_GAP_USER_TURNS = 2    # E3 2건 사이 USER 턴 ≥2
FAST_TRACK_AUTO_CONFIRM_DAYS = 14  # 14일 무F → 자동 확정(evaluate 시점 lazy)
FAST_TRACK_FAIL_EXPOSURES = 2    # 노출 기회 2회 F만 → PRACTICING 복귀
FAST_TRACK_REVERT_SCORE = 2.0

PLACEMENT_MASTERED_SCORE = 3.0
PLACEMENT_INTRODUCED_SCORE = 1.0
PLACEMENT_DEMOTE_SCORE = 1.0     # placement 는 실증거 F 1건에 즉시 굴복(⑩)

# 레벨업 게이트 파라미터 — 밴드별(mechanics ⑦ 표). G4 는 "미만" 비교.
# D12: G1/G2 는 문법(+L1 청크) 전용 — 어휘용 파라미터 없음(어휘는 게이트 미산입).
# D15: 체류 게이트(G3) 폐지 — G1/G2 와 이중 규제. G5 는 일수만(연쇄 승급 방지 안전핀).
_BAND_GATES: dict[str, dict] = {
    "survival": dict(g1=0.90, g2=0.50, g4=0.35, g5_days=3),
    "beginner": dict(g1=0.90, g2=0.55, g4=0.30, g5_days=7),
    "intermediate": dict(g1=0.85, g2=0.50, g4=0.25, g5_days=7),
    "advanced": dict(g1=0.80, g2=0.45, g4=0.20, g5_days=10),
}


def _band_of(level_no: int) -> str:
    if level_no <= 1:
        return "survival"
    if level_no <= 5:
        return "beginner"
    if level_no <= 9:
        return "intermediate"
    return "advanced"


# 레벨(1~13) → 게이트 파라미터. 모듈 상수 dict — 튜닝 지점.
GATE_PARAMS: dict[int, dict] = {lv: _BAND_GATES[_band_of(lv)] for lv in range(1, 14)}

G4_MIN_EVIDENCE = 10  # G4 분모 <10 이면 pass(표본 부족)
MAX_LEVEL = 13

_SUCCESS_GRADES = ("E1", "E2", "E3")  # 성공 증거(비F, E0 노출 제외)


# --------------------------------------------------------------------------- #
# 공용 헬퍼 (검증 게이트·해시 — normalcall_service._verify_detections 와 공유)
# --------------------------------------------------------------------------- #
def normalize_text(text: str | None) -> str:
    """공백 전제거 정규화 — 인용 부분일치·중복 dedup 의 공통 기준."""
    return "".join((text or "").split())


def text_hash(normalized: str) -> str:
    """정규화 인용문 sha1 — item_evidence.normalized_text_hash."""
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _word_jaccard(a: str, b: str) -> float:
    """어절(공백 분리) 집합 Jaccard — fast-track "서로 다른 문장" 판정."""
    sa, sb = set((a or "").split()), set((b or "").split())
    if not sa and not sb:
        return 1.0
    union = sa | sb
    return len(sa & sb) / len(union) if union else 1.0


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """naive(sqlite)/aware(postgres) 혼재 방어 — 비교 전 UTC aware 로 통일."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


@dataclass
class VerifiedEvidence:
    """서버 검증 게이트를 통과한 증거 1건 — apply_evidence 의 입력 단위.

    user_turn_ordinal(USER 턴 순번)·before_first_mention(비버 최초 언급 이전 발화)은
    검증 시점(전사 보유)에 계산해 붙인다 — fast-track 5조건이 전사 재조회 없이 판정되도록.
    """

    item_id: int
    kind: str                      # grammar / vocab / chunk (승격 상한 카테고리)
    grade_raw: str                 # LLM 원판정(E1/E2/E3/F)
    grade_final: str               # 검증 게이트 후 최종(E0/E1/E2/E3/F)
    quote: str
    turn_index: Optional[int]      # 전사 turn_index (item_evidence 기록)
    user_turn_ordinal: Optional[int]  # USER 턴만 센 순번(0부터) — "사이 USER 턴" 계산
    norm_hash: str
    injected: bool = False         # 이번 통화 프롬프트 주입 여부(c2 전엔 전부 False)
    before_first_mention: bool = True  # 비버가 그 항목을 처음 말하기 전의 발화인가


# --------------------------------------------------------------------------- #
# 증거 반영 + 상태 전이 (mechanics ⑤ 5~6단계 + ⑥)
# --------------------------------------------------------------------------- #
def apply_evidence(
    db: Session, member_id: int, call_id: int, verified: list[VerifiedEvidence],
    language: str = "ko",
) -> dict:
    """검증된 증거를 item_evidence 에 append 하고 체크판(progress)을 갱신한다.

    - 점수 Δ 반영(클램프 0~6, 통화당 항목별 순증 +2.0 상한) — 실반영값을 score_delta 에 기록.
    - 상태 전이(⑥ 표): UNSEEN→INTRODUCED(E0/E1) / →PRACTICING(E2+ 1건 || 서로 다른
      통화 2회 E1 근사) / →MASTERED(4조건 전부, chunk 특례) / fast-track(5조건).
    - 통화당 승격 상한: MASTERED 문법2·어휘6·청크4, 신규 INTRODUCED 8, fast-track 2.
    - commit 하지 않는다(호출부 단일 commit).

    Returns:
        {"evidence", "introduced", "practicing", "mastered", "fast_tracked",
         "fast_track_confirmed", "fast_track_reverted"} 요약 카운트.
    """
    summary = {
        "evidence": 0, "introduced": 0, "practicing": 0, "mastered": 0,
        "fast_tracked": 0, "fast_track_confirmed": 0, "fast_track_reverted": 0,
    }
    if not verified:
        return summary

    now = datetime.now(timezone.utc)
    today = now.date()

    # 항목별 그룹(검출 등장 순서 보존 — 승격 상한의 결정적 적용 순서)
    by_item: dict[int, list[VerifiedEvidence]] = {}
    for ev in verified:
        by_item.setdefault(ev.item_id, []).append(ev)

    item_ids = list(by_item)
    progress_map = mastery_repository.get_progress_map(db, member_id, item_ids)
    prior_by_item: dict[int, list[ItemEvidence]] = {}
    for row in mastery_repository.list_evidence_for_items(db, member_id, item_ids):
        prior_by_item.setdefault(row.item_id, []).append(row)

    introduced_new = 0
    mastered_promoted: dict[str, int] = {k: 0 for k in MASTERED_PROMOTION_CAPS}
    fast_tracked = 0

    for item_id, evs in by_item.items():
        kind = evs[0].kind
        grades = [ev.grade_final for ev in evs]
        has_f = "F" in grades
        has_e1 = "E1" in grades
        has_e2plus = any(g in ("E2", "E3") for g in grades)
        prog = progress_map.get(item_id)

        # ── UNSEEN → 행 생성 (통화당 신규 INTRODUCED 상한 8) ──
        if prog is None:
            if introduced_new >= INTRODUCED_CAP_PER_CALL:
                # 상한 초과: 행 없이 증거만 append(실반영 0) — 다음 통화 순환에서 재포착.
                for ev in evs:
                    db.add(_evidence_row(member_id, item_id, call_id, ev, 0.0, now, language=language))
                    summary["evidence"] += 1
                continue
            prog = MemberItemProgress(
                member_id=member_id, item_id=item_id,
                status="introduced", score=0.0, provenance="observed",
                repeat_count=0, prompted_count=0, spontaneous_count=0, miss_count=0,
                first_seen_at=now, last_seen_at=now, first_call_id=call_id,
            )
            db.add(prog)
            progress_map[item_id] = prog
            introduced_new += 1
            summary["introduced"] += 1

        # 전이 근사용 사전값(이 통화 반영 전) 캡처 — "서로 다른 통화 2회 E1"
        prev_repeat = prog.repeat_count or 0
        prev_last_call = prog.last_call_id

        # ── 점수·카운터·증거 append (순증 +2.0/통화, 클램프 0~6) ──
        net_gain = 0.0
        for ev in evs:
            delta = SCORE_DELTA.get(ev.grade_final, 0.0)
            if delta > 0:
                delta = min(delta, max(0.0, NET_GAIN_CAP_PER_CALL - net_gain))
            current = prog.score or 0.0
            new_score = min(SCORE_MAX, max(SCORE_MIN, current + delta))
            applied = round(new_score - current, 4)
            if applied > 0:
                net_gain += applied
            prog.score = new_score
            if ev.grade_final == "E1":
                prog.repeat_count = (prog.repeat_count or 0) + 1
            elif ev.grade_final == "E2":
                prog.prompted_count = (prog.prompted_count or 0) + 1
            elif ev.grade_final == "E3":
                prog.spontaneous_count = (prog.spontaneous_count or 0) + 1
            elif ev.grade_final == "F":
                prog.miss_count = (prog.miss_count or 0) + 1
            if ev.grade_final in ("E2", "E3"):
                prog.last_used_at = now
            db.add(_evidence_row(member_id, item_id, call_id, ev, applied, now, language=language))
            summary["evidence"] += 1
        prog.last_seen_at = now
        prog.last_call_id = call_id
        if prog.first_call_id is None:
            prog.first_call_id = call_id

        prior = prior_by_item.get(item_id, [])

        # ── 이미 MASTERED: fast-track 미확정 확정/복귀 + placement 굴복만 ──
        if prog.status == "mastered":
            if prog.provenance == "fast_track" and prog.fast_track_confirmed_at is None:
                if has_e2plus:
                    # 이후 통화에서 E2+ 1건 → 확정
                    prog.fast_track_confirmed_at = now
                    summary["fast_track_confirmed"] += 1
                elif has_f:
                    # F만 나온 노출 기회 — 누적 2회면 PRACTICING(score 2.0) 복귀
                    fails = _f_only_call_count(prior, after=prog.mastered_at) + 1
                    if fails >= FAST_TRACK_FAIL_EXPOSURES:
                        prog.status = "practicing"
                        prog.score = FAST_TRACK_REVERT_SCORE
                        prog.provenance = "observed"
                        summary["fast_track_reverted"] += 1
            elif prog.provenance == "placement" and has_f:
                # placement 는 실증거에 즉시 굴복(⑩): F 1건 → PRACTICING score 1.0
                prog.status = "practicing"
                prog.score = PLACEMENT_DEMOTE_SCORE
                prog.provenance = "observed"
            continue  # 정식 MASTERED(observed/확정 fast_track)는 강등 없음(⑥)

        # ── fast-track: 같은 통화 5조건 전부 → MASTERED(미확정) 직행 ──
        e3s = [ev for ev in evs if ev.grade_final == "E3"]
        if (
            len(e3s) >= 2
            and not has_f                                    # ⑤ F 0건
            and fast_tracked < FAST_TRACK_CAP_PER_CALL
            and mastered_promoted.get(kind, 0) < MASTERED_PROMOTION_CAPS.get(kind, 0)
            and _fast_track_pair_ok(e3s)                     # ②③ 문장 상이 + 사이 턴
            # ④ ≥1건 이 비버 최초 언급 이전 발화 — 미주입 항목은 자동 충족
            and any((not ev.injected) or ev.before_first_mention for ev in e3s)
        ):
            prog.status = "mastered"
            prog.provenance = "fast_track"
            prog.fast_track_confirmed_at = None  # 미확정 — G2 미산입, 리텐션 재확인 대상
            prog.mastered_at = now
            fast_tracked += 1
            mastered_promoted[kind] = mastered_promoted.get(kind, 0) + 1
            summary["fast_tracked"] += 1
            summary["mastered"] += 1
            continue

        # ── INTRODUCED → PRACTICING: E2+ 1건 || 서로 다른 통화 2회 E1(근사) ──
        if prog.status == "introduced":
            two_call_e1 = (
                has_e1 and prev_repeat >= 1
                and prev_last_call is not None and prev_last_call != call_id
            )
            if has_e2plus or two_call_e1:
                prog.status = "practicing"
                if prog.provenance == "placement":
                    prog.provenance = "observed"
                summary["practicing"] += 1

        # ── PRACTICING → MASTERED: 4조건 전부(⑥, chunk 특례) + 승격 상한 ──
        if prog.status == "practicing":
            if mastered_promoted.get(kind, 0) < MASTERED_PROMOTION_CAPS.get(kind, 0) and (
                _mastered_conditions_met(prog, kind, prior, evs, call_id, today)
            ):
                prog.status = "mastered"
                prog.provenance = "observed"
                prog.mastered_at = now
                mastered_promoted[kind] = mastered_promoted.get(kind, 0) + 1
                summary["mastered"] += 1

    return summary


def _evidence_row(
    member_id: int, item_id: int, call_id: int,
    ev: VerifiedEvidence, score_delta: float, now: datetime, language: str = "ko",
) -> ItemEvidence:
    """검증된 증거 1건 → ItemEvidence(append-only). created_at 을 명시해 판정 결정성 확보."""
    return ItemEvidence(
        member_id=member_id, language=language, item_id=item_id, call_id=call_id,
        turn_index=ev.turn_index,
        grade_raw=ev.grade_raw, grade_final=ev.grade_final,
        learner_quote=ev.quote, verified=True,
        score_delta=score_delta, normalized_text_hash=ev.norm_hash,
        created_at=now,
    )


def _fast_track_pair_ok(e3s: list[VerifiedEvidence]) -> bool:
    """fast-track 조건 ②③: E3 쌍 중 hash 상이 && 어절 Jaccard<0.5 && 사이 USER 턴≥2."""
    for i in range(len(e3s)):
        for j in range(i + 1, len(e3s)):
            a, b = e3s[i], e3s[j]
            if a.norm_hash == b.norm_hash:
                continue
            if _word_jaccard(a.quote, b.quote) >= FAST_TRACK_JACCARD_MAX:
                continue
            if a.user_turn_ordinal is None or b.user_turn_ordinal is None:
                continue
            if abs(a.user_turn_ordinal - b.user_turn_ordinal) - 1 < FAST_TRACK_GAP_USER_TURNS:
                continue
            return True
    return False


def _f_only_call_count(prior: list[ItemEvidence], after: Optional[datetime]) -> int:
    """fast-track 승격 이후, 그 항목이 F만 남긴 통화 수 — 복귀(2회) 판정 재료."""
    cutoff = _as_utc(after)
    by_call: dict[int, list[str]] = {}
    for row in prior:
        created = _as_utc(row.created_at)
        if cutoff is not None and created is not None and created < cutoff:
            continue
        by_call.setdefault(row.call_id, []).append(row.grade_final)
    return sum(1 for grades in by_call.values() if grades and all(g == "F" for g in grades))


def _mastered_conditions_met(
    prog: MemberItemProgress,
    kind: str,
    prior: list[ItemEvidence],
    evs: list[VerifiedEvidence],
    call_id: int,
    today,
) -> bool:
    """MASTERED 4조건(⑥): ①score≥3.0 ②성공증거 2통화·2일 분산 ③성공 산출(E2/E3)
    3회 이상(D14 — 따라말하기 E1 미산입), 그중 E3≥1 (chunk 특례: 2회·E2 인정 — E3 불요,
    L1 생존 레벨은 짧은 설계) ④최근 증거≠F. 증거 로그 집계로 판정."""
    # ① 점수
    if (prog.score or 0.0) < MASTERED_MIN_SCORE:
        return False
    # ④ 최근 증거 ≠ F (이번 통화 마지막 증거 기준 — 항상 이번 통화 증거가 최신)
    if evs[-1].grade_final == "F":
        return False
    # ② 성공 증거(E1+) 서로 다른 통화 2회 && 다른 날 2일 (★ 게이밍 방어 — 고정)
    success_calls = {r.call_id for r in prior if r.grade_final in _SUCCESS_GRADES}
    success_dates = {
        d.date() for d in (_as_utc(r.created_at) for r in prior if r.grade_final in _SUCCESS_GRADES)
        if d is not None
    }
    if any(ev.grade_final in _SUCCESS_GRADES for ev in evs):
        success_calls.add(call_id)
        success_dates.add(today)
    if len(success_calls) < 2 or len(success_dates) < 2:
        return False
    # ③ 성공 산출(E2/E3) — 문법·어휘 3회 이상(D14), chunk 2회(L1 단기 설계 특례).
    #    따라말하기(E1)는 '말한 횟수'에 미산입, 그중 E3(자발)≥1 은 비-chunk 유지.
    e2plus = sum(1 for r in prior if r.grade_final in ("E2", "E3"))
    e2plus += sum(1 for ev in evs if ev.grade_final in ("E2", "E3"))
    min_productions = 2 if kind == "chunk" else MASTERED_MIN_PRODUCTIONS
    if e2plus < min_productions:
        return False
    if kind != "chunk":
        e3 = sum(1 for r in prior if r.grade_final == "E3")
        e3 += sum(1 for ev in evs if ev.grade_final == "E3")
        if e3 < 1:
            return False
    return True


# --------------------------------------------------------------------------- #
# 레벨업 판정 (mechanics ⑦ — 멱등 3중)
# --------------------------------------------------------------------------- #
def evaluate_level_up(db: Session, member_id: int, trigger_call_id: int, language: str = "ko") -> dict:
    """통화 분석 커밋 흐름에서 호출되는 레벨업 게이트 판정(commit 은 호출부).

    승급은 문법(+L1 청크) 전용 — D12: G1/G2 분모(list_gate_items)에 어휘가 없다.
    어휘 증거·상태는 계속 쌓이지만 승급 판정엔 산입하지 않는다.

    게이트 = G1(배움) && G2(잘씀) && G4(오류율) && G5(승급 후 일수 잠금) — D15 로
    체류 게이트(G3) 폐지. G4 의 창은 "최근 5 증거통화"(item_evidence 에 행이 있는
    distinct call, 최신순 — 분모 <10 이면 pass).

    멱등 3중: ⓪ history 에 trigger_call_id 존재 → 스킵(UNIQUE 멱등 키)
             ① member FOR UPDATE(동시 분석 경합 방지) ② 항상 +1(다단 승급 금지).

    Returns:
        {"result": "skipped"|"stay"|"promoted", ...} + gate 스냅샷.
    """
    # ⓪ 멱등 키
    if mastery_repository.get_history_by_trigger(db, trigger_call_id) is not None:
        return {"result": "skipped", "reason": "already_evaluated"}

    # ① 경합 방지
    member = mastery_repository.get_member_for_update(db, member_id)
    if member is None or member.korean_level is None:
        return {"result": "skipped", "reason": "no_member_or_level"}
    k = member.korean_level
    if k >= MAX_LEVEL:
        return {"result": "skipped", "reason": "max_level"}

    now = datetime.now(timezone.utc)

    # fast-track 14일 무F → 자동 확정(lazy — evaluate 시점에 일괄 판정·기록)
    auto_confirmed = 0
    for prog in mastery_repository.list_unconfirmed_fast_track(db, member_id):
        mastered_at = _as_utc(prog.mastered_at)
        if mastered_at is None or (now - mastered_at).days < FAST_TRACK_AUTO_CONFIRM_DAYS:
            continue
        if not mastery_repository.has_f_evidence_since(
            db, member_id, prog.item_id, prog.mastered_at
        ):
            prog.fast_track_confirmed_at = now
            auto_confirmed += 1

    params = GATE_PARAMS[k]

    # 집계 — 전부 현재 레벨 k 항목 한정. 분모는 문법(+L1 청크)만 — 어휘 제외(D12).
    gate_items = mastery_repository.list_gate_items(db, k)
    denom = len(gate_items)
    if denom == 0:
        # 커리큘럼 미시드 방어 — 게이트 대상이 없으면 승급 불가(데이터 준비 후 재개)
        return {"result": "stay", "reason": "no_gate_items", "level": k}

    prog_map = mastery_repository.get_progress_map(db, member_id, [i for i, _ in gate_items])
    introduced_plus = sum(
        1 for p in prog_map.values() if p.status in ("introduced", "practicing", "mastered")
    )
    mastered_confirmed = sum(
        1 for p in prog_map.values()
        if p.status == "mastered"
        and (p.provenance != "fast_track" or p.fast_track_confirmed_at is not None)
    )

    # 레벨 진입 시각: history 최신 행, 없으면 placement 폴백 = 가입(created_at)
    latest = mastery_repository.get_latest_history(db, member_id)
    entry_at = _as_utc(latest.created_at if latest else member.created_at) or now
    entry_reason = latest.reason if latest else None
    days_in_level = (now - entry_at).days

    # G4 — 최근 5 증거통화(D15: 증거가 있는 distinct call)의 F비율
    #      (현재 레벨 항목 한정, 분모 <10 이면 pass)
    recent_ids = mastery_repository.list_recent_evidence_call_ids(db, member_id, limit=5)
    f_count, ev_total = mastery_repository.evidence_grade_counts(db, member_id, recent_ids, k)
    f_ratio = (f_count / ev_total) if ev_total else 0.0

    g1_ratio = introduced_plus / denom
    g2_ratio = mastered_confirmed / denom
    g1_pass = g1_ratio >= params["g1"]
    g2_pass = g2_ratio >= params["g2"]
    g4_pass = ev_total < G4_MIN_EVIDENCE or f_ratio < params["g4"]
    # G5 잠금 — 게이트 승급 직후에만 적용(D15: 일수만 — 연쇄 승급 방지 안전핀)
    g5_pass = True
    if latest is not None and latest.reason == "gate_promotion":
        g5_pass = days_in_level >= params["g5_days"]

    snapshot = {
        "level": k,
        "gate_scope": "grammar_chunk",  # D12 — 분모에 어휘 없음(문법+L1 청크 전용)
        "denominator": denom,
        "introduced_plus": introduced_plus,
        "mastered_confirmed": mastered_confirmed,
        "g1": {"ratio": round(g1_ratio, 4), "threshold": params["g1"], "pass": g1_pass},
        "g2": {"ratio": round(g2_ratio, 4), "threshold": params["g2"], "pass": g2_pass},
        "g4": {
            "f_count": f_count, "evidence_total": ev_total,
            "ratio": round(f_ratio, 4), "threshold": params["g4"], "pass": g4_pass,
        },
        "g5": {
            "entry_reason": entry_reason, "days": days_in_level,
            "days_required": params["g5_days"], "pass": g5_pass,
        },
        "fast_track_auto_confirmed": auto_confirmed,
        "evaluated_at": now.isoformat(),
    }

    if not (g1_pass and g2_pass and g4_pass and g5_pass):
        return {"result": "stay", "snapshot": snapshot}

    # ② 항상 +1 — 단일 트랜잭션(호출부 commit)에 레벨 + history 합류
    member.korean_level = k + 1
    db.add(
        MemberLevelHistory(
            member_id=member_id,
            language=language,
            from_level=k,
            to_level=k + 1,
            reason="gate_promotion",
            trigger_call_id=trigger_call_id,
            gate_snapshot=json.dumps(snapshot, ensure_ascii=False),
            created_at=now,
        )
    )
    logger.info(
        "레벨업: member=%s %d→%d (trigger_call=%s g1=%.2f g2=%.2f)",
        member_id, k, k + 1, trigger_call_id, g1_ratio, g2_ratio,
    )
    return {"result": "promoted", "from_level": k, "to_level": k + 1, "snapshot": snapshot}


# --------------------------------------------------------------------------- #
# grandfathering (mechanics ⑩ — 레벨테스트 배정 직후 체크판 초기화)
# --------------------------------------------------------------------------- #
def apply_grandfathering(
    db: Session,
    member_id: int,
    level_no: int,
    *,
    trigger_call_id: Optional[int] = None,
    from_level: Optional[int] = None,
    language: str = "ko",
) -> dict:
    """레벨 k 배정 시 하위 레벨 항목을 placement 로 선반영한다(commit 은 호출부).

    - ≤k−2 항목 → MASTERED(placement, score 3.0) / k−1 → INTRODUCED(placement, score 1.0)
      / ≥k → UNSEEN(행 없음). placement 는 기존 행을 덮지 않는다(행 부재 시만 insert) —
      실증거(observed)가 이미 있으면 그것이 우선.
    - 범위 판단(구현 결정): ⑩ 은 "≤k−2 전 항목"이라 하지만 non-core 어휘(노출 풀)까지
      행을 만들면 k=13 기준 ~9천 행 insert 가 된다. 행 부재=UNSEEN 이 기본이므로
      **grammar 전체+chunk+core 어휘만** 행을 생성한다(D12 로 어휘가 게이트에서 빠진
      뒤에도 core 어휘 placement 는 유지 — 복습·재교육 방지 목적, 승급 판정과 무관).
      grammar 는 45 초과 '선택 문법'도 포함(재교육 방지 — 재활용 풀).
    - member_level_history 에 reason='placement' 1행 기록(trigger_call_id=해당 통화) —
      이 행이 "레벨 진입 시각"의 단일 소스가 된다.

    Returns:
        {"mastered": 생성 행 수, "introduced": 생성 행 수}.
    """
    now = datetime.now(timezone.utc)
    existing = mastery_repository.existing_progress_item_ids(db, member_id)

    created_mastered = 0
    if level_no >= 3:  # k-2 >= 1 일 때만 대상 존재
        for item_id in mastery_repository.list_grandfather_item_ids(
            db, max_level=level_no - 2
        ):
            if item_id in existing:
                continue
            db.add(
                MemberItemProgress(
                    member_id=member_id, item_id=item_id,
                    status="mastered", provenance="placement",
                    score=PLACEMENT_MASTERED_SCORE,
                    repeat_count=0, prompted_count=0, spontaneous_count=0, miss_count=0,
                    first_seen_at=now, last_seen_at=now, mastered_at=now,
                )
            )
            created_mastered += 1

    created_introduced = 0
    if level_no >= 2:  # k-1 >= 1
        for item_id in mastery_repository.list_grandfather_item_ids(
            db, exact_level=level_no - 1
        ):
            if item_id in existing:
                continue
            db.add(
                MemberItemProgress(
                    member_id=member_id, item_id=item_id,
                    status="introduced", provenance="placement",
                    score=PLACEMENT_INTRODUCED_SCORE,
                    repeat_count=0, prompted_count=0, spontaneous_count=0, miss_count=0,
                    first_seen_at=now, last_seen_at=now,
                )
            )
            created_introduced += 1

    db.add(
        MemberLevelHistory(
            member_id=member_id,
            language=language,
            from_level=from_level,
            to_level=level_no,
            reason="placement",
            trigger_call_id=trigger_call_id,
            created_at=now,
        )
    )
    logger.info(
        "grandfathering: member=%s level=%d mastered=%d introduced=%d",
        member_id, level_no, created_mastered, created_introduced,
    )
    return {"mastered": created_mastered, "introduced": created_introduced}
