"""cur_* 서비스 — 표현학습·프리토킹 통화의 «지금 차시» 선별·판정 저장·차시 완료·프리토킹 해금. **트랜잭션 경계(commit)는 여기.**

계획(정본) docs/plans/2026-09-12-cur-2단계-통화경로-이전.md — §2 데이터 흐름 · §6 codex ①~④ · §7 fable P0·P1-1~7·ⓑ · §8 코스 결정.
선별 규칙 docs/20260912_0330_cur-스키마-설계.md §11 (사장님 확정 2026-09-12).

⛔⛔ 원칙 — cur_* + member + character + call 만 읽고 쓴다. 옛 학습 테이블은 이름조차 적지 않는다(모델 독스트링).
⛔ 판정은 하지 않는다 — passed/failed 는 통화(T16 서버 판정)가 준 사실을 **적을 뿐**이다. 여기서 뜻·표면형으로 다시 가르지 않는다.

## 멱등·경로 고정 (§6 ②③ · §7 P0)
  · call_id 가 멱등 키. `cur_call` 은 통화 시작에 «없으면» INSERT — 있으면(continues_call_id 조각 재개) INSERT 생략·잠금 검사 면제·
    **그 cur_call 의 차시**로 재선별(포인터가 넘어갔어도 이 통화는 그 차시다).
  · `record_expression` 은 `cur_call.recorded_at IS NULL` 일 때만 쓰고 채운다 — 재분석·중복 종료 = no-op.
  · `complete_freetalk` 는 이미 done 이면 no-op. 조각마다 불려도 한 번만 찍힌다.

## 판정 축 (§6 ① · §5)
  회원 항목 기록의 PK 는 (회원, 차시, 항목)이다. 복습으로 실린 다른 차시 항목의 판정은 **그 항목의 차시 행**에 적는다 — DTO 가
  항목마다 `lesson_id` 를 싣고(§6 ①), 통화는 그걸 `state.expr_items[*]["lesson_id"]` 로 운반하며, record 는 (member, item.lesson_id,
  item_id) 로 UPSERT 한다. 문법이 두 차시에 속하면 이번 목록에 실린 쪽 차시.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from core.config import settings
from domains.learning.models.curriculum import (
    CurCall,
    CurItem,
    CurLesson,
    CurMemberItem,
    CurMemberLesson,
    CurMemberProgress,
)
from domains.learning.repository import curriculum_repository as repo

logger = logging.getLogger(__name__)

COURSE_EXPRESSION = "expression"
COURSE_FREETALK = "freetalk"
STATUS_LEARNING = "learning"
STATUS_EXPRESSION_DONE = "expression_done"
STATUS_FREETALK_DONE = "freetalk_done"
# 프리토킹 «1회 = 완료» 하한(§7 ⓑ) — 3초 오접속이 그 차시의 유일한 프리토킹을 태우지 않게.
FREETALK_MIN_DURATION_S = 60.0
# 프리토킹 브리프에 싣는 그 차시 표현 상한(복습 유도용).


class CourseLocked(Exception):
    """프리토킹이 아직 안 열렸다(그 차시 표현학습 미완). B2 가 ServerError(code="COURSE_LOCKED") 로 바꾼다."""

    def __init__(self, lesson_code: str, status: str):
        super().__init__(f"course locked: lesson={lesson_code} status={status}")
        self.lesson_code = lesson_code
        self.status = status


@dataclass
class CurFreetalkBrief:
    """프리토킹 지시문 재료 — `build_freetalk_instruction(lesson=…)` 에 넣는다(계획 2026-09-12-프리토킹-코스-대본 §5).

    items: 차시 항목 **전부**(상한 없음) `[{obj, ex, role}]` — 문형(grammar)은 예문과 함께(«이름을 말하지 말고 문장으로»), ex 는
      `_example(item, seen_count)` 회전. surfaces 는 호환용(obj 목록) — 새 코드는 items 를 본다.
    """

    situation: str
    partner: Optional[str]
    surfaces: list[str] = field(default_factory=list)
    probes: list[str] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)


@dataclass
class CurCallOpen:
    """통화 시작 결과. items 는 `build_expression_instruction(items=…)` 스키마(obj/des/ex) + lesson_id·role·review."""

    lesson: CurLesson
    course: str
    items: list[dict]
    brief: Optional[CurFreetalkBrief]
    resumed: bool            # cur_call 이 이미 있었다(조각 재개) — INSERT 0
    status: str              # 그 차시의 회원 상태(learning / expression_done / freetalk_done)
    # ⭐ 강제 프리토킹(admin QA 우회, 2026-09-12): 잠금을 건너 열렸다 → 종료 시 complete_freetalk 를 부르지 않는다(진도 무영향).
    #   재개(조각2)에도 그대로 — 저장 컬럼 없이 «프리토킹인데 status 가 expression_done 이 아니다» 로 되짚는다(정상 프리토킹은 열릴 때
    #   expression_done 이었고, 조각1 이 이미 끝냈으면 freetalk_done — 그때 complete 는 어차피 no-op).
    forced: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_list(raw: Optional[str]) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return v if isinstance(v, list) else []


def _meaning(item: CurItem, locale: str) -> Optional[str]:
    if not item.meanings:
        return None
    try:
        m = json.loads(item.meanings)
    except (ValueError, TypeError):
        return None
    if not isinstance(m, dict):
        return None
    return m.get(locale) or m.get("en") or None


def _example(item: CurItem, seen_count: int) -> Optional[str]:
    """예문 1개 — 몇 번째 보는지로 회전(§11: 첫 드릴 = 예문1, 첫 복습 = 예문2 …). 랜덤 아님. 없으면 None."""
    exs = [e for e in _json_list(item.examples) if isinstance(e, str) and e.strip()]
    if not exs:
        return None
    return exs[max(0, int(seen_count)) % len(exs)]


def _dto(item: CurItem, lesson_id: int, role: str, *, seen_count: int, review: bool, locale: str) -> dict:
    return {
        "item_id": int(item.item_id),
        "lesson_id": int(lesson_id),
        "obj": item.surface,
        "des": _meaning(item, locale),
        "ex": _example(item, seen_count),
        "role": role,
        "review": bool(review),
    }


# ── 진도 ─────────────────────────────────────────────────────────────────── #
def available(db: Session, language: str = "ko") -> bool:
    """cur 경로를 탈 수 있나 — 시드(cur_lesson no=1)가 있어야 한다. 없으면 호출부가 옛 경로로 떨어진다(R5 — 스위치만 켜진 빈 DB 에서
    통화가 죽지 않게). 운영 DB 는 1단계에서 적재됐다."""
    return repo.lesson_by_no(db, language, 1) is not None


def ensure_progress(db: Session, member_id: int, language: str = "ko", *, for_update: bool = False) -> CurMemberProgress:
    """회원의 «지금 차시» 행 — 없으면 no=1 로 만든다(레벨테스트→시작 차시 연결은 별건). 만들면 commit."""
    prog = repo.current_progress(db, member_id, language, for_update=for_update)
    if prog is not None:
        return prog
    first = repo.lesson_by_no(db, language, 1)
    if first is None:
        raise RuntimeError("cur_lesson 이 비어 있다 — 시드 적재(scripts/curriculum/load_cur_seed.py) 먼저")
    prog = CurMemberProgress(member_id=member_id, language=language, lesson_id=first.lesson_id)
    db.add(prog)
    db.commit()
    # commit 뒤 다시 읽어 잠금까지 원자적으로(동시 start 두 개가 각자 만들려다 하나가 IntegrityError 면 그쪽이 재시도한다)
    return repo.current_progress(db, member_id, language, for_update=for_update) or prog


def _status_of(db: Session, member_id: int, lesson_id: int) -> str:
    row = repo.lesson_status(db, member_id, lesson_id)
    return row.status if row is not None else STATUS_LEARNING


def decide_course(db: Session, member_id: int, language: str = "ko") -> str:
    """§8 auto — 지금 차시가 expression_done 이고 프리토킹 미완이면 freetalk, 아니면 expression."""
    prog = ensure_progress(db, member_id, language)
    return COURSE_FREETALK if _status_of(db, member_id, prog.lesson_id) == STATUS_EXPRESSION_DONE else COURSE_EXPRESSION


# ── 통화 시작 ─────────────────────────────────────────────────────────────── #
def select_items(db: Session, member_id: int, lesson_id: int, *, locale: str = "en", n: Optional[int] = None) -> list[dict]:
    """§11 선별 — ① 지금 차시의 안 배운 항목 seq 순 [:N] ② 부족분은 복습(어느 차시든, 뒤에 붙임). 한 통화는 한 차시 안."""
    n = int(n if n is not None else settings.CUR_ITEMS_PER_CALL)
    mine = repo.member_item_map(db, member_id, lesson_id)
    fresh: list[dict] = []
    for li, it in repo.lesson_items(db, lesson_id):
        rec = mine.get(it.item_id)
        if rec is not None and rec.drilled_at is not None:
            continue
        fresh.append(_dto(it, lesson_id, li.role, seen_count=(rec.seen_count if rec else 0), review=False, locale=locale))
        if len(fresh) >= n:
            break
    out = list(fresh)
    if len(out) < n:
        exclude = frozenset(d["item_id"] for d in out)
        for mi, it in repo.review_pool(db, member_id, exclude, n - len(out)):
            # 복습 항목의 role 은 그 차시에서의 역할 — 없으면(비정상) kind 로
            role = repo.lesson_item_role(db, mi.lesson_id, it.item_id) or it.kind
            out.append(_dto(it, mi.lesson_id, role, seen_count=mi.seen_count, review=True, locale=locale))
    return out


def _brief(db: Session, lesson: CurLesson, member_id: Optional[int] = None) -> CurFreetalkBrief:
    """차시 항목 전부를 소재로(상한 폐기 — 계획 §9 정정). member_id 가 있으면 예문을 seen_count 로 회전(표현학습과 같은 예문 순서)."""
    mine = repo.member_item_map(db, member_id, lesson.lesson_id) if member_id is not None else {}
    items: list[dict] = []
    for li, it in repo.lesson_items(db, lesson.lesson_id):
        rec = mine.get(it.item_id)
        items.append({
            "obj": it.surface,
            "ex": _example(it, rec.seen_count if rec is not None else 0),
            "role": li.role,
        })
    probes = [p for p in _json_list(lesson.probes) if isinstance(p, str) and p.strip()]
    return CurFreetalkBrief(
        situation=lesson.situation, partner=lesson.partner,
        surfaces=[d["obj"] for d in items], probes=probes, items=items,
    )


def open_call(
    db: Session, member_id: int, call_id: int, course: str, *, language: str = "ko", locale: str = "en", force: bool = False,
) -> CurCallOpen:
    """통화 시작 — cur_call «없으면» INSERT(P0-4·P1-4), 있으면 조각 재개(§7 P0: 잠금 면제·그 차시로 재선별).

    course: "expression" | "freetalk" | "auto"(§8 — 서버가 정한다).
    프리토킹인데 그 차시가 expression_done 이 아니면 CourseLocked(새 통화만 — 재개는 면제).
    force(2026-09-12 QA 우회): course=="freetalk" 이고 **회원이 admin** 이면 잠금을 건너 지금 차시로 연다(CurCallOpen.forced=True —
      호출부가 종료 훅을 건너 진도를 안 건드린다). admin 이 아니면 조용히 무시(잠금 그대로). auto·expression 엔 영향 없다.
    """
    existing = repo.cur_call(db, call_id)
    if existing is not None:
        lesson = repo.lesson_by_id(db, existing.lesson_id)
        assert lesson is not None
        status = _status_of(db, member_id, lesson.lesson_id)
        items = select_items(db, member_id, lesson.lesson_id, locale=locale) if existing.course == COURSE_EXPRESSION else []
        brief = _brief(db, lesson, member_id) if existing.course == COURSE_FREETALK else None
        forced = existing.course == COURSE_FREETALK and status != STATUS_EXPRESSION_DONE
        logger.info("cur open_call: 조각 재개 call_id=%s lesson=%s course=%s 재선별=%d%s", call_id, lesson.code, existing.course, len(items),
                    " 강제(진도 무영향)" if forced else "")
        return CurCallOpen(lesson=lesson, course=existing.course, items=items, brief=brief, resumed=True, status=status, forced=forced)

    prog = ensure_progress(db, member_id, language, for_update=True)
    lesson = repo.lesson_by_id(db, prog.lesson_id)
    assert lesson is not None
    status = _status_of(db, member_id, lesson.lesson_id)
    if course == "auto":
        course = COURSE_FREETALK if status == STATUS_EXPRESSION_DONE else COURSE_EXPRESSION
    if course not in (COURSE_EXPRESSION, COURSE_FREETALK):
        raise ValueError(f"unknown course: {course!r}")
    forced = False
    if course == COURSE_FREETALK and status != STATUS_EXPRESSION_DONE:
        if force and repo.member_role(db, member_id) == "admin":
            forced = True
            logger.info("cur 프리토킹 강제(admin) lesson=%s(no=%d) status=%s member=%s call_id=%s — 진도 무영향", lesson.code, lesson.no, status, member_id, call_id)
        else:
            db.rollback()   # FOR UPDATE 잠금 해제
            raise CourseLocked(lesson.code, status)
    db.add(CurCall(call_id=call_id, lesson_id=lesson.lesson_id, course=course))
    db.commit()
    items = select_items(db, member_id, lesson.lesson_id, locale=locale) if course == COURSE_EXPRESSION else []
    brief = _brief(db, lesson, member_id) if course == COURSE_FREETALK else None
    logger.info(
        "cur open_call: call_id=%s member=%s lesson=%s(no=%d) course=%s status=%s 항목=%d(복습 %d)",
        call_id, member_id, lesson.code, lesson.no, course, status, len(items), sum(1 for d in items if d["review"]),
    )
    return CurCallOpen(lesson=lesson, course=course, items=items, brief=brief, resumed=False, status=status, forced=forced)


# ── 통화 종료(표현학습) ────────────────────────────────────────────────────── #
def _snapshot_rows(items: Iterable[dict], drilled: set[int], passed: set[int], failed: set[int]) -> list[dict]:
    """결과 화면 스냅샷 행 — 앱(7f70c29)이 그리는 quiz_items 5키(item_id·surface·meaning·passed·failed) + role·drilled."""
    rows = []
    for d in items:
        iid = int(d["item_id"])
        ok = iid in passed
        rows.append({
            "item_id": iid,
            "role": d.get("role"),
            "surface": d.get("obj") or "",
            "meaning": d.get("des"),
            "drilled": iid in drilled,
            "passed": ok,
            "failed": (iid in failed) and not ok,
            "review": bool(d.get("review")),
        })
    return rows


def merge_call_items(existing_raw: Optional[str], incoming: list[dict]) -> list[dict]:
    """cur_call.items 병합(P1-5) — item_id 기준 합집합, passed/failed OR(단 passed 면 failed=False), 표면형·뜻은 최신, drilled OR,
    review OR(하네스가 복습 식별에 쓴다 — B3 §8). 옛 `_merge_expression_snapshot` 규칙 그대로 — 조각2 가 조각1 의 통과분을 지우지 않게."""
    merged: dict[int, dict] = {}
    for rows in (_json_list(existing_raw), incoming):
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                iid = int(r.get("item_id") or 0)
            except (TypeError, ValueError):
                continue
            if not iid:
                continue
            prev = merged.get(iid) or {}
            passed = bool(r.get("passed")) or bool(prev.get("passed"))
            merged[iid] = {
                "item_id": iid,
                "role": r.get("role") or prev.get("role"),
                "surface": str(r.get("surface") or prev.get("surface") or ""),
                "meaning": r.get("meaning") or prev.get("meaning") or None,
                "drilled": bool(r.get("drilled")) or bool(prev.get("drilled")),
                "passed": passed,
                "failed": (bool(r.get("failed")) or bool(prev.get("failed"))) and not passed,
                "review": bool(r.get("review")) or bool(prev.get("review")),
            }
    return list(merged.values())


def record_expression(
    db: Session,
    call_id: int,
    items: list[dict],
    drilled_ids: Iterable[int],
    passed_ids: Iterable[int],
    failed_ids: Iterable[int],
    snapshot: Optional[list[dict]] = None,
) -> Optional[dict]:
    """표현학습 통화 종료 저장 — `cur_call.recorded_at IS NULL` 일 때만(§6 ②). 두 번 부르면 None(no-op).

    items: 통화가 운반한 DTO 그대로(lesson_id 포함) — 어느 차시 행에 적을지가 여기서 나온다(§6 ①).
    Returns: {"lesson_completed": bool, "status": str, "drilled": n, "passed": n, "failed": n} | None
    """
    cc = repo.cur_call(db, call_id)
    if cc is None:
        logger.warning("cur record_expression: cur_call 없음 call_id=%s — 옛 경로 통화거나 시작이 안 찍혔다(무시)", call_id)
        return None
    if cc.recorded_at is not None:
        logger.info("cur record_expression: 이미 저장됨 call_id=%s recorded_at=%s (no-op)", call_id, cc.recorded_at)
        return None
    if cc.course != COURSE_EXPRESSION:
        logger.warning("cur record_expression: course=%s 통화에 표현학습 저장 요청(무시) call_id=%s", cc.course, call_id)
        return None
    member_id = repo.call_member_id(db, call_id)
    if member_id is None:
        logger.warning("cur record_expression: call 행 없음 call_id=%s(무시)", call_id)
        return None

    drilled = {int(x) for x in drilled_ids}
    passed = {int(x) for x in passed_ids}
    failed = {int(x) for x in failed_ids}
    now = _now()

    for d in items:
        iid = int(d["item_id"])
        lid = int(d.get("lesson_id") or cc.lesson_id)
        row = repo.member_item(db, member_id, lid, iid)
        if row is None:
            row = CurMemberItem(member_id=member_id, lesson_id=lid, item_id=iid)
            db.add(row)
        row.seen_count = int(row.seen_count or 0) + 1                      # 목록에 실린 전부
        if iid in drilled and row.drilled_at is None:                        # 단조
            row.drilled_at = now
            row.drilled_call_id = call_id
        if iid in passed and row.quiz_passed_at is None:                     # 단조
            row.quiz_passed_at = now
        if iid in failed and row.quiz_passed_at is None:                     # 미통과일 때만 오답 횟수
            row.quiz_failed_count = int(row.quiz_failed_count or 0) + 1
        if iid in passed or iid in failed:
            row.last_quiz_call_id = call_id
    db.flush()

    # 결과 화면 스냅샷 — item_id 병합(P1-5)
    rows = snapshot if snapshot is not None else _snapshot_rows(items, drilled, passed, failed)
    cc.items = json.dumps(merge_call_items(cc.items, rows), ensure_ascii=False)

    # 차시 완료 = 그 차시 cur_lesson_item(현역) 전부 drilled (결정 #4 — 퀴즈 정오 무관)
    lesson_item_ids = {it.item_id for _li, it in repo.lesson_items(db, cc.lesson_id)}
    mine = repo.member_item_map(db, member_id, cc.lesson_id)
    completed = bool(lesson_item_ids) and all(
        (mine.get(i) is not None and mine[i].drilled_at is not None) for i in lesson_item_ids
    )
    cc.lesson_completed = completed

    ml = repo.lesson_status(db, member_id, cc.lesson_id)
    if ml is None:
        ml = CurMemberLesson(member_id=member_id, lesson_id=cc.lesson_id, status=STATUS_LEARNING)
        db.add(ml)
    ml.expression_calls = int(ml.expression_calls or 0) + 1                  # «조각 수»(P1-5 정의)
    if completed and ml.status == STATUS_LEARNING:
        ml.status = STATUS_EXPRESSION_DONE
    if completed and ml.expression_done_at is None:                          # 단조
        ml.expression_done_at = now
    cc.recorded_at = now
    db.commit()
    logger.info(
        "cur record_expression: call_id=%s lesson=%s 항목 %d(드릴 %d·통과 %d·오답 %d) 완료=%s status=%s calls=%d",
        call_id, cc.lesson_id, len(items), len(drilled), len(passed), len(failed), completed, ml.status, ml.expression_calls,
    )
    return {"lesson_completed": completed, "status": ml.status, "drilled": len(drilled), "passed": len(passed), "failed": len(failed)}


# ── 통화 종료(프리토킹) ────────────────────────────────────────────────────── #
def complete_freetalk(db: Session, call_id: int, duration_s: float, normal_end: bool) -> Optional[dict]:
    """프리토킹 1회 = 완료(§7 ⓑ: 정상 종료 이고 길이 ≥ 60초). freetalk_done + 포인터 = 다음 차시(MIN(no) > 현재, 없으면 유지). 멱등."""
    cc = repo.cur_call(db, call_id)
    if cc is None or cc.course != COURSE_FREETALK:
        return None
    if not normal_end or float(duration_s or 0) < FREETALK_MIN_DURATION_S:
        logger.info("cur complete_freetalk: 미완료 처리 call_id=%s normal_end=%s duration=%.0fs", call_id, normal_end, duration_s or 0)
        return {"freetalk_done": False, "moved": False}
    member_id = repo.call_member_id(db, call_id)
    if member_id is None:
        return None
    ml = repo.lesson_status(db, member_id, cc.lesson_id)
    if ml is not None and ml.status == STATUS_FREETALK_DONE:
        return {"freetalk_done": True, "moved": False}                        # 이미 done — no-op(조각2)
    now = _now()
    if ml is None:
        ml = CurMemberLesson(member_id=member_id, lesson_id=cc.lesson_id, status=STATUS_LEARNING)
        db.add(ml)
    if ml.expression_done_at is None:                                        # CHECK 정합 — 프리토킹까지 왔으면 표현학습은 끝난 것
        ml.expression_done_at = now
    ml.status = STATUS_FREETALK_DONE
    ml.freetalk_done_at = now
    ml.freetalk_call_id = call_id
    moved = False
    prog = repo.current_progress(db, member_id)
    lesson = repo.lesson_by_id(db, cc.lesson_id)
    if prog is not None and lesson is not None and prog.lesson_id == cc.lesson_id:
        nxt = repo.next_lesson(db, lesson.language, lesson.no)
        if nxt is not None:
            prog.lesson_id = nxt.lesson_id
            moved = True
    db.commit()
    logger.info("cur complete_freetalk: call_id=%s lesson=%s freetalk_done 포인터이동=%s", call_id, cc.lesson_id, moved)
    return {"freetalk_done": True, "moved": moved}


# ── 조회·dev ─────────────────────────────────────────────────────────────── #
def me(db: Session, member_id: int, language: str = "ko") -> dict:
    """GET /cur/me (§2) — {lesson:{no,code,level_no,situation,topic}, status, items_total, items_drilled, open:{expression, freetalk}}."""
    prog = ensure_progress(db, member_id, language)
    lesson = repo.lesson_by_id(db, prog.lesson_id)
    assert lesson is not None
    status = _status_of(db, member_id, lesson.lesson_id)
    items_total = len(repo.lesson_items(db, lesson.lesson_id))
    return {
        "lesson": {
            "no": lesson.no, "code": lesson.code, "level_no": lesson.level_no,
            "situation": lesson.situation, "topic": _topic_name(db, lesson),
        },
        "status": status,
        "items_total": items_total,
        "items_drilled": repo.drilled_count(db, member_id, lesson.lesson_id),
        # 표현학습은 언제나 열려 있다(복습 통화도 표현학습이다). 프리토킹은 그 차시 표현학습이 끝났고 아직 안 했을 때만.
        "open": {"expression": True, "freetalk": status == STATUS_EXPRESSION_DONE},
        # ⭐ 홈 화면(사장님 ⑪) — auto 로 걸면 서버가 정할 코스. decide_course 와 같은 규칙(expression_done 이면 freetalk).
        "next_course": decide_course(db, member_id, language),
    }


def _topic_name(db: Session, lesson: CurLesson) -> Optional[str]:
    if lesson.topic_id is None:
        return None
    from domains.learning.models.curriculum import CurTopic
    t = db.get(CurTopic, lesson.topic_id)
    return t.name if t is not None else None


def lessons(db: Session, member_id: int, level: Optional[int] = None, language: str = "ko") -> list[dict]:
    """GET /cur/lessons?level= — [{no, code, level_no, situation, status}] (내 상태 조인 · 안 시작한 차시는 status None)."""
    mine = repo.member_lessons(db, member_id)
    prog = repo.current_progress(db, member_id, language)
    out = []
    for l in repo.lessons(db, language, level):
        row = mine.get(l.lesson_id)
        status = row.status if row is not None else (STATUS_LEARNING if prog is not None and prog.lesson_id == l.lesson_id else None)
        out.append({"no": l.no, "code": l.code, "level_no": l.level_no, "situation": l.situation, "status": status})
    return out


def reset(db: Session, member_id: int, lesson_no: Optional[int] = None, language: str = "ko") -> dict:
    """POST /__dev/cur-reset — 그 회원의 cur_member_item / cur_member_lesson / cur_call 삭제 + 포인터를 lesson_no(기본 1)로."""
    from sqlalchemy import delete
    deleted_calls = repo.member_call_ids(db, member_id)
    if deleted_calls:
        db.execute(delete(CurCall).where(CurCall.call_id.in_(deleted_calls)))
    db.execute(delete(CurMemberItem).where(CurMemberItem.member_id == member_id))
    db.execute(delete(CurMemberLesson).where(CurMemberLesson.member_id == member_id))
    target = repo.lesson_by_no(db, language, int(lesson_no or 1))
    if target is None:
        db.rollback()
        raise ValueError(f"lesson no={lesson_no} 없음")
    prog = repo.current_progress(db, member_id, language)
    if prog is None:
        db.add(CurMemberProgress(member_id=member_id, language=language, lesson_id=target.lesson_id))
    else:
        prog.lesson_id = target.lesson_id
    db.commit()
    return {"member_id": member_id, "lesson_no": target.no, "lesson_code": target.code, "deleted_calls": len(deleted_calls)}
